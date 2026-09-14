"""HTTP layer.

Nothing here does slow work. Uploads stream to disk chunk by chunk; probing,
transcription, suggestion and rendering are all handed to the worker through
the jobs table. The longest thing a request does is write one 8 MB chunk.
"""
from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import auth, db, edits, jobs
from app.config import settings
from app.naming import zip_filename
from app.timestamps import MIN_CLIP_SECONDS, TimestampError, format_timestamp, parse_cut_list

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["timestamp"] = lambda value: format_timestamp(float(value or 0))


def human_bytes(value: Any) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


templates.env.filters["human_bytes"] = human_bytes

app = FastAPI(title="Clipstory", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    problems = settings.validate()
    for problem in problems:
        log.error("configuration problem: %s", problem)
    if problems:
        raise RuntimeError("refusing to start with an unsafe configuration: " + "; ".join(problems))
    settings.ensure_dirs()
    db.wait_for_database()
    db.apply_schema()
    log.info("clipstory web ready at %s", settings.base_url)


# ------------------------------------------------------------ dependencies --

def current_user(request: Request) -> dict[str, Any] | None:
    return auth.user_from_session(request.cookies.get(auth.SESSION_COOKIE))


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="admin only")
    return user


# Endpoints the browser calls with fetch() rather than by navigating. Errors on
# these must come back as JSON so the page's own error handling can read them;
# everywhere else a human is looking at a browser window and wants a page.
JSON_PATH_PREFIXES = ("/api/", "/healthz")
JSON_PATH_SUFFIXES = ("/status", ".json", "/apply", "/state", "/edit", "/cleanup", "/export")


def wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith(JSON_PATH_PREFIXES) or path.endswith(JSON_PATH_SUFFIXES)


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    """Send browsers to the login page rather than showing them raw JSON."""
    if wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "error.html",
        {"user": current_user(request), "code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )


# -------------------------------------------------------------------- auth --

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, sent: bool = False, error: str | None = None):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"user": None, "sent": sent, "error": error}
    )


@app.post("/login")
def login_submit(email: str = Form(...)):
    try:
        url = auth.request_login_link(email)
        log.info("login link issued for %s", auth.normalise_email(email))
        if not settings.resend_api_key:
            log.warning("SIGN-IN LINK for %s: %s", auth.normalise_email(email), url)
    except auth.NotInvited:
        # Deliberately indistinguishable from success. Reflecting "not invited"
        # would let anyone with the URL enumerate who is on the team.
        log.warning("login attempted by uninvited address %s", auth.normalise_email(email))
    return RedirectResponse("/login?sent=1", status_code=303)


@app.get("/auth/verify")
def verify(token: str):
    try:
        user = auth.consume_login_token(token)
    except (auth.InvalidToken, auth.NotInvited):
        return RedirectResponse(
            "/login?error=That+link+is+invalid+or+has+expired.+Request+a+new+one.",
            status_code=303,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_session(user),
        max_age=settings.session_ttl_days * 86400, httponly=True, samesite="lax",
        secure=settings.base_url.startswith("https://"),
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# -------------------------------------------------------------------- jobs --

@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        request, "jobs.html", {"user": user, "jobs": jobs.list_jobs(user)}
    )


@app.get("/jobs/new", response_class=HTMLResponse)
def new_job(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        request, "new.html",
        {"user": user, "chunk_bytes": settings.upload_chunk_bytes,
         "max_bytes": settings.max_upload_bytes},
    )


def _job_context(job: dict, user: dict, **extra: Any) -> dict[str, Any]:
    job_id = str(job["id"])
    clips = jobs.get_clips(job_id)
    editable = job["status"] in jobs.EDITABLE
    return {
        "user": user,
        "job": job,
        "clips": clips,
        "transcript": jobs.get_transcript(job_id),
        "suggestions": job.get("suggestions") or [],
        "editable": editable,
        "retention_days": settings.clip_retention_days,
        "suggestions_enabled": settings.suggestions_enabled,
        # The source is deleted on finalise, so playback is only offered while
        # the file is actually still there.
        "source_available": bool(job["source_path"]) and not job["source_deleted_at"],
        # Everything the review page's script needs, as one JSON blob.
        "review_data": {
            "jobId": job_id,
            "duration": float(job["duration_seconds"] or 0),
            "editable": editable,
            "words": jobs.get_word_boundaries(job_id) if editable else [],
            "clips": [
                {
                    "id": c["id"], "sequence": c["sequence"], "label": c["label"] or "",
                    "start": float(c["start_seconds"]), "end": float(c["end_seconds"]),
                    "status": c["status"], "filename": c["output_filename"],
                    "error": c["error"],
                }
                for c in clips
            ],
        },
        **extra,
    }


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    return templates.TemplateResponse(request, "job.html", _job_context(job, user))


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str, user: dict = Depends(require_user)):
    """Polled by the job page so progress updates without a refresh."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    clips = jobs.get_clips(job_id)
    return {
        "status": job["status"],
        "error": job["error"],
        "clip_count": len(clips),
        "clips_done": sum(1 for c in clips if c["status"] == jobs.CLIP_COMPLETE),
        "clips": [{"id": c["id"], "sequence": c["sequence"], "status": c["status"]} for c in clips],
    }


@app.get("/jobs/{job_id}/source")
def stream_source(job_id: str, user: dict = Depends(require_user)):
    """The source video, for scrubbing beside the transcript. FileResponse
    honours Range requests, which is what lets the player seek."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    if not job["source_path"] or job["source_deleted_at"]:
        raise HTTPException(410, "the source video has been deleted")
    path = jobs.storage().path_for(job["source_path"])
    if not path.exists():
        raise HTTPException(410, "the source video is no longer on disk")
    return FileResponse(path, media_type="video/mp4")


# ------------------------------------------------------------------ upload --

@app.post("/api/uploads")
def upload_init(
    filename: str = Form(...), total_bytes: int = Form(...),
    user: dict = Depends(require_user),
):
    """Open an upload. The browser then sends the file in chunks.

    Chunked rather than one large POST because these files are gigabytes: a
    single request that fails at 90% would have to start over, and many proxies
    refuse bodies that large outright.
    """
    if total_bytes <= 0:
        raise HTTPException(400, "file appears to be empty")
    if total_bytes > settings.max_upload_bytes:
        raise HTTPException(
            413, f"file is {human_bytes(total_bytes)}, over the "
                 f"{human_bytes(settings.max_upload_bytes)} limit",
        )
    free = jobs.storage().free_bytes()
    # Room for the source plus its clips, with margin. Refusing here gives a
    # clear message instead of an ffmpeg failure halfway through a render.
    if free < total_bytes * 1.5:
        raise HTTPException(
            507, f"not enough disk space: {human_bytes(free)} free, "
                 f"about {human_bytes(total_bytes * 1.5)} needed",
        )
    job = jobs.create_job(user["id"], Path(filename).name, total_bytes)
    return {"job_id": str(job["id"]), "chunk_bytes": settings.upload_chunk_bytes}


@app.get("/api/uploads/{job_id}")
def upload_status(job_id: str, user: dict = Depends(require_user)):
    """How many bytes we already hold, so an interrupted upload can resume."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "upload not found")
    return {"received_bytes": int(job["received_bytes"]),
            "total_bytes": int(job["source_bytes"]), "status": job["status"]}


@app.put("/api/uploads/{job_id}")
async def upload_chunk(job_id: str, request: Request, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "upload not found")
    try:
        offset = int(request.headers.get("x-chunk-offset", ""))
    except ValueError:
        raise HTTPException(400, "missing or invalid X-Chunk-Offset header")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty chunk")
    try:
        received = jobs.append_upload_chunk(job_id, offset, data)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"received_bytes": received, "total_bytes": int(job["source_bytes"])}


@app.post("/api/uploads/{job_id}/complete")
def upload_complete(job_id: str, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "upload not found")
    try:
        jobs.finish_upload(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"job_id": job_id, "status": jobs.UPLOADED}


# ------------------------------------------------------------------ review --

class ClipRow(BaseModel):
    id: int | None = None
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    label: str | None = None


class ApplyRequest(BaseModel):
    rows: list[ClipRow]


def _validate_rows(rows: list[ClipRow], duration: float | None) -> list[jobs.ClipEdit]:
    """The same rules the text parser applies, for rows arriving as JSON."""
    edits = []
    for number, row in enumerate(rows, start=1):
        if row.start >= row.end:
            raise HTTPException(400, f"clip {number}: start must be before end")
        if row.end - row.start < MIN_CLIP_SECONDS:
            raise HTTPException(400, f"clip {number}: shorter than {MIN_CLIP_SECONDS}s")
        if duration and row.end > duration + 0.001:
            raise HTTPException(
                400, f"clip {number}: ends at {format_timestamp(row.end)}, past the "
                     f"end of the video ({format_timestamp(duration)})",
            )
        label = (row.label or "").strip() or None
        edits.append(jobs.ClipEdit(start=round(row.start, 3), end=round(row.end, 3),
                                   label=label, id=row.id))
    return edits


@app.post("/jobs/{job_id}/apply")
def apply_edits(job_id: str, body: ApplyRequest, user: dict = Depends(require_user)):
    """The review page's save. Only clips that changed get re-rendered."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    edits = _validate_rows(body.rows, job["duration_seconds"])
    try:
        counts = jobs.apply_clip_edits(job_id, edits)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"counts": counts, "status": jobs.get_job(job_id)["status"]}


@app.post("/jobs/{job_id}/cuts", response_class=HTMLResponse)
def submit_cuts(
    job_id: str, request: Request, cuts: str = Form(...),
    user: dict = Depends(require_user),
):
    """The text-box path. Parsed with the same rules, then applied as edits, so
    a line that exactly matches an existing clip leaves it untouched."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    duration = float(job["duration_seconds"]) if job["duration_seconds"] else None
    try:
        ranges = parse_cut_list(cuts, source_duration=duration)
    except TimestampError as exc:
        return templates.TemplateResponse(
            request, "job.html",
            _job_context(job, user, cuts_text=cuts, parse_error=exc.message,
                         error_line_number=exc.line_number, error_line=exc.line),
            status_code=400,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "job.html",
            _job_context(job, user, cuts_text=cuts, parse_error=str(exc)),
            status_code=400,
        )
    try:
        jobs.apply_clip_edits(
            job_id, [jobs.ClipEdit(start=r.start, end=r.end, label=r.label) for r in ranges]
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/suggest")
def rerun_suggestions(job_id: str, user: dict = Depends(require_user)):
    """Queue another suggestion pass. The worker does it; nothing waits here."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    if not jobs.get_transcript(job_id):
        raise HTTPException(409, "this job has no transcript to analyse")
    try:
        jobs.request_suggestions(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# -------------------------------------------------------------------- edit --

class DeletionRow(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    reason: str = "manual"


class SaveEditRequest(BaseModel):
    deletions: list[DeletionRow]


class CleanupRequest(BaseModel):
    remove_fillers: bool = True
    shorten_silences: bool = True
    include_conversational: bool = False


@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_page(job_id: str, request: Request, user: dict = Depends(require_user)):
    """The transcript as a document. Striking words out here edits the video."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    jobs.ensure_edit(job_id)
    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "user": user,
            "job": job,
            "edit": jobs.get_edit(job_id),
            "edit_state": jobs.edit_state(job),
            "editable": job["status"] in jobs.EDITABLE,
            "source_available": bool(job["source_path"]) and not job["source_deleted_at"],
            "has_transcript": bool(jobs.get_transcript(job_id)),
            "retention_days": settings.clip_retention_days,
            "silence_threshold": settings.silence_threshold_seconds,
            "silence_target": settings.silence_target_seconds,
        },
    )


@app.post("/jobs/{job_id}/edit")
def save_edit(job_id: str, body: SaveEditRequest, user: dict = Depends(require_user)):
    """Autosave the document. Cheap, frequent, and never queues a render."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] not in jobs.EDITABLE:
        raise HTTPException(409, f"cannot edit while the job is {job['status']}")

    duration = float(job["duration_seconds"] or 0)
    deletions = []
    for number, row in enumerate(body.deletions, start=1):
        if row.start >= row.end:
            raise HTTPException(400, f"deletion {number}: start must be before end")
        if duration and row.end > duration + 0.001:
            raise HTTPException(400, f"deletion {number}: past the end of the video")
        deletions.append(edits.Deletion(start=row.start, end=row.end, reason=row.reason))

    # Deliberately not a plain save. The editor can only describe deletions
    # that strike out words, so shortened pauses have to be preserved
    # server-side or every autosave would quietly undo them.
    jobs.save_edit_from_words(job_id, deletions)
    return jobs.edit_state(jobs.get_job(job_id))


@app.get("/jobs/{job_id}/edit/state")
def edit_state(job_id: str, user: dict = Depends(require_user)):
    """Polled while an export runs, so the page updates without a refresh."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    return jobs.edit_state(job) | {"job_status": job["status"]}


@app.post("/jobs/{job_id}/edit/cleanup")
def edit_cleanup(job_id: str, body: CleanupRequest, user: dict = Depends(require_user)):
    """Run the automatic passes, keeping the operator's own deletions."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] not in jobs.EDITABLE:
        raise HTTPException(409, f"cannot edit while the job is {job['status']}")

    vocabulary = None
    if body.include_conversational:
        # Opt-in only. Each of these is usually load-bearing in a sentence, so
        # removing them by default would silently change what someone said.
        vocabulary = set(edits.DEFAULT_FILLERS) | set(edits.OPTIONAL_FILLERS)

    try:
        return jobs.apply_cleanup(
            job_id,
            remove_fillers=body.remove_fillers,
            shorten_silences=body.shorten_silences,
            filler_vocabulary=vocabulary,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/jobs/{job_id}/edit/export")
def edit_export(job_id: str, user: dict = Depends(require_user)):
    """Queue the export. The worker encodes; nothing waits in this request."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        jobs.request_edit_render(job_id)
    except edits.EditError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"status": jobs.get_job(job_id)["status"]}


@app.get("/jobs/{job_id}/edit/download")
def edit_download(job_id: str, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    edit = jobs.get_edit(job_id)
    if edit is None or not edit["output_path"]:
        raise HTTPException(404, "this job has no exported edit")
    path = jobs.storage().path_for(edit["output_path"])
    if not path.exists():
        raise HTTPException(410, "this export has been deleted under the retention policy")
    return FileResponse(path, media_type="video/mp4", filename=edit["output_filename"])


@app.post("/jobs/{job_id}/finalise")
def finalise(job_id: str, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        jobs.finalise_job(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# ---------------------------------------------------------------- download --

@app.get("/jobs/{job_id}/clips/{clip_id}")
def download_clip(job_id: str, clip_id: int, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    clip = db.query_one("SELECT * FROM clips WHERE id = %s AND job_id = %s", (clip_id, job_id))
    if clip is None or not clip["output_path"]:
        raise HTTPException(404, "clip not found")
    path = jobs.storage().path_for(clip["output_path"])
    if not path.exists():
        raise HTTPException(410, "this clip has been deleted under the retention policy")
    return FileResponse(path, media_type="video/mp4", filename=clip["output_filename"])


@app.get("/jobs/{job_id}/manifest.json")
def download_manifest(job_id: str, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(jobs.build_manifest(job),
                        headers={"Content-Disposition": 'attachment; filename="manifest.json"'})


@app.get("/jobs/{job_id}/download.zip")
def download_zip(job_id: str, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    clips = [c for c in jobs.get_clips(job_id) if c["status"] == jobs.CLIP_COMPLETE]
    if not clips:
        raise HTTPException(404, "this job has no rendered clips")

    store = jobs.storage()
    # Built on demand into a temp file and deleted once sent, so a batch never
    # occupies disk twice for longer than the download takes. MP4 is already
    # compressed, so ZIP_STORED avoids pointless CPU on a 2-core box.
    handle = tempfile.NamedTemporaryFile(suffix=".zip", dir=settings.data_dir, delete=False)
    handle.close()
    archive_path = Path(handle.name)
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
            for clip in clips:
                source = store.path_for(clip["output_path"])
                if source.exists():
                    archive.write(source, arcname=clip["output_filename"])
            archive.writestr("manifest.json", jobs.manifest_bytes(job))
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive_path, media_type="application/zip",
        filename=zip_filename(job["source_filename"]),
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


# ------------------------------------------------------------------- admin --

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(
        request, "admin.html",
        {"user": user, "invites": auth.list_invites(),
         "email_configured": bool(settings.resend_api_key)},
    )


@app.post("/admin/invite")
def admin_invite(email: str = Form(...), user: dict = Depends(require_admin)):
    auth.add_invite(email, user["email"])
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/revoke")
def admin_revoke(email: str = Form(...), user: dict = Depends(require_admin)):
    if auth.normalise_email(email) == user["email"]:
        raise HTTPException(400, "you cannot revoke your own access")
    auth.remove_invite(email)
    return RedirectResponse("/admin", status_code=303)


@app.get("/healthz")
def healthz(response: Response):
    """Liveness for the tunnel and for any uptime check."""
    try:
        db.query_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": str(exc)[:200]}
    return {"status": "ok"}
