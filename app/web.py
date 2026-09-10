"""HTTP layer.

Nothing here does slow work. Uploads stream to disk chunk by chunk, and probing
and rendering are handed to the worker through the jobs table. The longest
thing a request does is write one 8 MB chunk.
"""
from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from app import auth, db, jobs
from app.config import settings
from app.naming import clip_filename, zip_filename
from app.suggest import SuggestionError, suggest_clips
from app.timestamps import (
    TimestampError,
    find_overlaps,
    format_timestamp,
    parse_cut_list,
)

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
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
JSON_PATH_SUFFIXES = ("/status", ".json")


def wants_json(request: Request) -> bool:
    path = request.url.path
    if path.startswith(JSON_PATH_PREFIXES) or path.endswith(JSON_PATH_SUFFIXES):
        return True
    # Deciding by path rather than by the Accept header, because Accept varies
    # between browsers, fetch() calls and command-line clients, and getting it
    # wrong means a signed-out person sees raw JSON instead of a login page.
    return False


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    """Send browsers to the login page rather than showing them raw JSON."""
    if wants_json(request):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "error.html",
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
        auth.SESSION_COOKIE,
        auth.issue_session(user),
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
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
        request,
        "new.html",
        {"user": user,
         "chunk_bytes": settings.upload_chunk_bytes,
         "max_bytes": settings.max_upload_bytes},
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    return templates.TemplateResponse(
        request, "job.html", _job_context(job, user)
    )


def _job_context(job: dict, user: dict) -> dict[str, Any]:
    job_id = str(job["id"])
    return {
        "user": user,
        "job": job,
        "clips": jobs.get_clips(job_id),
        "transcript": jobs.get_transcript(job_id),
        "suggestions": job.get("suggestions") or [],
        "retention_days": settings.clip_retention_days,
        "suggestions_enabled": settings.suggestions_enabled,
        # The source is deleted once clips render, so playback is only offered
        # while the file is actually still there.
        "source_available": bool(job["source_path"]) and not job["source_deleted_at"],
    }


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str, user: dict = Depends(require_user)):
    """Polled by the job page so progress updates without a refresh."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    clips = jobs.get_clips(job_id)
    done = sum(1 for c in clips if c["status"] == "complete")
    return {
        "status": job["status"],
        "error": job["error"],
        "transcript_ready": bool(job["transcript_language"]) or bool(job["transcript_error"]),
        "clip_count": len(clips),
        "clips_done": done,
        "clips": [
            {"sequence": c["sequence"], "status": c["status"],
             "filename": c["output_filename"], "error": c["error"]}
            for c in clips
        ],
    }


# ------------------------------------------------------------------ upload --

@app.post("/api/uploads")
def upload_init(
    filename: str = Form(...),
    total_bytes: int = Form(...),
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
            413,
            f"file is {human_bytes(total_bytes)}, over the "
            f"{human_bytes(settings.max_upload_bytes)} limit",
        )

    free = jobs.storage().free_bytes()
    # Room for the source plus its clips, with margin. Refusing here gives a
    # clear message instead of an ffmpeg failure halfway through a render.
    if free < total_bytes * 1.5:
        raise HTTPException(
            507,
            f"not enough disk space: {human_bytes(free)} free, "
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
    return {
        "received_bytes": int(job["received_bytes"]),
        "total_bytes": int(job["source_bytes"]),
        "status": job["status"],
    }


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


# -------------------------------------------------------------------- cuts --

@app.post("/jobs/{job_id}/cuts", response_class=HTMLResponse)
def submit_cuts(
    job_id: str,
    request: Request,
    cuts: str = Form(...),
    confirm: str = Form(default=""),
    user: dict = Depends(require_user),
):
    """Parse the pasted cut list, show it for confirmation, then queue it.

    Two passes on purpose. The operator sees exactly what was understood before
    a single frame is rendered, which is where a mistyped timestamp gets caught.
    """
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")

    duration = float(job["duration_seconds"]) if job["duration_seconds"] else None
    context = _job_context(job, user) | {"cuts_text": cuts}

    try:
        ranges = parse_cut_list(cuts, source_duration=duration)
    except TimestampError as exc:
        context |= {"parse_error": exc.message, "error_line_number": exc.line_number,
                    "error_line": exc.line}
        return templates.TemplateResponse(request, "job.html", context, status_code=400)
    except ValueError as exc:
        context |= {"parse_error": str(exc)}
        return templates.TemplateResponse(request, "job.html", context, status_code=400)

    if confirm != "yes":
        # Show the filenames the renderer will actually produce, computed with
        # the same function it uses. Anything approximated here would be a
        # promise the render does not keep.
        preview = [
            (cut, clip_filename(job["source_filename"], cut.sequence, len(ranges), cut.label))
            for cut in ranges
        ]
        context |= {"preview": preview, "overlaps": find_overlaps(ranges)}
        return templates.TemplateResponse(request, "job.html", context)

    jobs.set_cuts(job_id, ranges)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}/source")
def stream_source(job_id: str, user: dict = Depends(require_user)):
    """Serve the source video so the operator can scrub it beside the transcript.

    FileResponse honours Range requests, which is what lets the player seek
    without downloading gigabytes first.
    """
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    if not job["source_path"] or job["source_deleted_at"]:
        raise HTTPException(410, "the source video has been deleted")
    path = jobs.storage().path_for(job["source_path"])
    if not path.exists():
        raise HTTPException(410, "the source video is no longer on disk")
    return FileResponse(path, media_type="video/mp4")


@app.post("/jobs/{job_id}/suggest")
def rerun_suggestions(job_id: str, request: Request, user: dict = Depends(require_user)):
    """Ask the model again. Cheap, and useful when the first pass missed."""
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")

    segments = jobs.transcript_segments_for(job_id)
    if not segments:
        raise HTTPException(409, "this job has no transcript to analyse")

    try:
        suggestions = suggest_clips(segments, target_count=settings.suggestion_count)
        jobs.save_suggestions(job_id, suggestions, None)
    except SuggestionError as exc:
        jobs.save_suggestions(job_id, [], str(exc))

    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# ---------------------------------------------------------------- download --

@app.get("/jobs/{job_id}/clips/{clip_id}")
def download_clip(job_id: str, clip_id: int, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    clip = db.query_one(
        "SELECT * FROM clips WHERE id = %s AND job_id = %s", (clip_id, job_id)
    )
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
    return JSONResponse(
        jobs.build_manifest(job),
        headers={"Content-Disposition": 'attachment; filename="manifest.json"'},
    )


@app.get("/jobs/{job_id}/download.zip")
def download_zip(job_id: str, user: dict = Depends(require_user)):
    job = jobs.get_job_for_user(job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")

    clips = [c for c in jobs.get_clips(job_id) if c["status"] == "complete"]
    if not clips:
        raise HTTPException(404, "this job has no rendered clips")

    store = jobs.storage()
    # Built on demand into a temp file and deleted once sent, so a batch never
    # occupies disk twice for longer than the download takes. MP4 is already
    # compressed, so ZIP_STORED avoids pointless CPU on a 2-core box.
    handle = tempfile.NamedTemporaryFile(
        suffix=".zip", dir=settings.data_dir, delete=False
    )
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
        archive_path,
        media_type="application/zip",
        filename=zip_filename(job["source_filename"]),
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


# ------------------------------------------------------------------- admin --

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(
        request,
        "admin.html",
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
    """Liveness for the tunnel and for any uptime check.

    Cheap on purpose: the web process never blocks on ffmpeg, so if this stops
    answering something is genuinely wrong.
    """
    try:
        db.query_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": str(exc)[:200]}
    return {"status": "ok"}
