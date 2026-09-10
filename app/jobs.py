"""Job lifecycle: upload, probe, transcribe, suggest, render, tweak, finalise.

Status flow::

    uploading -> uploaded -> probing -> transcribing -> queued -> rendering -> complete
                                            |                        ^           |
                                            | (no suggestions)       |           | apply changes
                                            v                        |           v
                                       awaiting_cuts ----------------+        (queued again,
                                                                               only changed
    complete/awaiting_cuts -> suggest_requested -> suggesting -> back           clips render)

    any -> failed         (recorded, source kept, always editable)
    complete -> finalised (renumbered, source deleted)
    old      -> expired   (files purged after retention)

Everything slow happens in the worker. State lives in Postgres, so a restart
resumes rather than losing the job. The default path needs no human input:
suggestions are rendered as soon as they exist. A human only steps in to
change something.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from psycopg.types.json import Json

from app import db
from app.config import settings
from app.media import CutVerificationError, MediaError, cut_clip, extract_audio, probe
from app.naming import clip_filename
from app.storage import LocalDiskStorage, Storage
from app.suggest import (
    SuggestionError,
    get_provider as get_suggestion_provider,
    suggest_clips,
    suggestions_to_json,
)
from app.timestamps import CutRange, format_timestamp
from app.transcription import (
    TranscriptSegment,
    TranscriptionError,
    Word,
    get_provider as get_transcription_provider,
)

log = logging.getLogger(__name__)

UPLOADING = "uploading"
UPLOADED = "uploaded"
PROBING = "probing"
TRANSCRIBING = "transcribing"
AWAITING_CUTS = "awaiting_cuts"
QUEUED = "queued"
RENDERING = "rendering"
COMPLETE = "complete"
FAILED = "failed"
SUGGEST_REQUESTED = "suggest_requested"
SUGGESTING = "suggesting"
FINALISED = "finalised"
EXPIRED = "expired"

# Statuses the worker picks up, and what each becomes once claimed.
CLAIM_TRANSITIONS = {
    UPLOADED: PROBING,
    QUEUED: RENDERING,
    SUGGEST_REQUESTED: SUGGESTING,
}

# Statuses a job is "in flight" under a worker, and where it returns to if that
# worker dies. transcribing returns to uploaded so probe + transcribe rerun
# from a clean start; a half-written transcript is not worth resuming.
STALE_TRANSITIONS = {
    PROBING: UPLOADED,
    TRANSCRIBING: UPLOADED,
    RENDERING: QUEUED,
    SUGGESTING: SUGGEST_REQUESTED,
}

# A job left mid-flight by a crashed or restarted worker is returned to the
# queue after this long. Generous, because a legitimate long transcription
# must not be stolen by a second worker while it is still going.
STALE_CLAIM_MINUTES = 240

# Statuses in which the review page lets the operator edit clips.
EDITABLE = (AWAITING_CUTS, COMPLETE, FAILED)

CLIP_PENDING = "pending"
CLIP_RENDERING = "rendering"
CLIP_COMPLETE = "complete"
CLIP_FAILED = "failed"


def storage() -> Storage:
    return LocalDiskStorage(settings.data_dir)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _touch(job_id: str) -> None:
    db.execute("UPDATE jobs SET updated_at = now() WHERE id = %s", (job_id,))


def set_status(job_id: str, status: str, error: str | None = None) -> None:
    db.execute(
        """
        UPDATE jobs SET status = %s, error = %s, claimed_by = NULL, claimed_at = NULL,
                        updated_at = now()
        WHERE id = %s
        """,
        (status, error, job_id),
    )


# ---------------------------------------------------------------- upload ----

def create_job(user_id: int, filename: str, total_bytes: int) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    source_key = f"sources/{job_id}/{filename}"
    return db.query_one(
        """
        INSERT INTO jobs (id, user_id, source_filename, source_path, source_bytes, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (job_id, user_id, filename, source_key, total_bytes, UPLOADING),
    )


def append_upload_chunk(job_id: str, offset: int, data: bytes) -> int:
    """Append one chunk, returning the new received byte count.

    Chunks must arrive in order and at the expected offset. A mismatched offset
    is rejected rather than written, which is what makes a resumed upload safe:
    the client asks how many bytes we have and continues from exactly there.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("no such job")
    if job["status"] != UPLOADING:
        raise ValueError(f"job is {job['status']}, not accepting uploads")

    received = int(job["received_bytes"])
    if offset != received:
        raise ValueError(f"expected chunk at offset {received}, got {offset}")
    if received + len(data) > int(job["source_bytes"]):
        raise ValueError("chunk would exceed the declared file size")

    with storage().open_write(job["source_path"], append=True) as handle:
        handle.write(data)

    new_total = received + len(data)
    db.execute(
        "UPDATE jobs SET received_bytes = %s, updated_at = now() WHERE id = %s",
        (new_total, job_id),
    )
    return new_total


def finish_upload(job_id: str) -> dict[str, Any]:
    """Mark the upload complete, after checking the bytes actually landed."""
    job = get_job(job_id)
    if job is None:
        raise ValueError("no such job")

    on_disk = storage().size(job["source_path"])
    expected = int(job["source_bytes"])
    if on_disk != expected:
        set_status(job_id, FAILED, f"upload incomplete: {on_disk} of {expected} bytes")
        raise ValueError(f"upload incomplete: {on_disk} of {expected} bytes on disk")

    set_status(job_id, UPLOADED)
    return get_job(job_id)


# ----------------------------------------------------------------- reads ----

def get_job(job_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))


def get_job_for_user(job_id: str, user: dict[str, Any]) -> dict[str, Any] | None:
    job = get_job(job_id)
    if job is None:
        return None
    if job["user_id"] != user["id"] and not user["is_admin"]:
        return None
    return job


def list_jobs(user: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    if user["is_admin"]:
        return db.query(
            """
            SELECT j.*, u.email AS owner_email,
                   (SELECT count(*) FROM clips c WHERE c.job_id = j.id) AS clip_count
            FROM jobs j JOIN users u ON u.id = j.user_id
            ORDER BY j.created_at DESC LIMIT %s
            """,
            (limit,),
        )
    return db.query(
        """
        SELECT j.*, %s AS owner_email,
               (SELECT count(*) FROM clips c WHERE c.job_id = j.id) AS clip_count
        FROM jobs j WHERE j.user_id = %s
        ORDER BY j.created_at DESC LIMIT %s
        """,
        (user["email"], user["id"], limit),
    )


def get_clips(job_id: str) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM clips WHERE job_id = %s ORDER BY sequence", (job_id,))


def get_transcript(job_id: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT idx, start_seconds, end_seconds, text
        FROM transcript_segments WHERE job_id = %s ORDER BY idx
        """,
        (job_id,),
    )


def get_word_boundaries(job_id: str) -> list[float]:
    """Every word start and end, sorted, for snapping cut points to speech."""
    rows = db.query(
        "SELECT words FROM transcript_segments WHERE job_id = %s ORDER BY idx", (job_id,)
    )
    boundaries: set[float] = set()
    for row in rows:
        for word in row["words"] or []:
            boundaries.add(float(word["s"]))
            boundaries.add(float(word["e"]))
    return sorted(boundaries)


def transcript_segments_for(job_id: str) -> list[TranscriptSegment]:
    """Rebuild provider objects from the database, for re-running suggestions."""
    return [
        TranscriptSegment(
            index=row["idx"],
            start=float(row["start_seconds"]),
            end=float(row["end_seconds"]),
            text=row["text"],
        )
        for row in get_transcript(job_id)
    ]


# ----------------------------------------------------------------- queue ----

def claim_next_job(worker_id: str) -> dict[str, Any] | None:
    """Atomically take the oldest available job.

    ``FOR UPDATE SKIP LOCKED`` is what makes this safe to run from more than
    one worker: each transaction locks a different row instead of queuing up
    behind the same one.
    """
    return db.query_one(
        """
        UPDATE jobs SET
            status = CASE status
                WHEN %s THEN %s
                WHEN %s THEN %s
                WHEN %s THEN %s
                ELSE status END,
            claimed_by = %s,
            claimed_at = now(),
            updated_at = now()
        WHERE id = (
            SELECT id FROM jobs
            -- = ANY(...) rather than IN (...): psycopg does not expand a
            -- Python tuple into an IN list, it binds it as a single value.
            WHERE status = ANY(%s)
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        (
            UPLOADED, CLAIM_TRANSITIONS[UPLOADED],
            QUEUED, CLAIM_TRANSITIONS[QUEUED],
            SUGGEST_REQUESTED, CLAIM_TRANSITIONS[SUGGEST_REQUESTED],
            worker_id,
            list(CLAIM_TRANSITIONS),
        ),
    )


def release_stale_claims() -> int:
    """Return jobs abandoned by a dead worker to the queue."""
    with db.connection() as conn:
        cursor = conn.execute(
            """
            UPDATE jobs SET
                status = CASE status
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    WHEN %s THEN %s
                    ELSE status END,
                claimed_by = NULL, claimed_at = NULL, updated_at = now()
            WHERE status = ANY(%s)
              AND claimed_at < now() - make_interval(mins => %s)
            """,
            (
                PROBING, STALE_TRANSITIONS[PROBING],
                TRANSCRIBING, STALE_TRANSITIONS[TRANSCRIBING],
                RENDERING, STALE_TRANSITIONS[RENDERING],
                SUGGESTING, STALE_TRANSITIONS[SUGGESTING],
                list(STALE_TRANSITIONS),
                STALE_CLAIM_MINUTES,
            ),
        )
        return cursor.rowcount or 0


# ----------------------------------------------------------------- probe ----

def probe_job(job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    source = storage().path_for(job["source_path"])
    try:
        info = probe(source)
    except MediaError as exc:
        log.error("job %s failed to probe: %s", job_id, exc)
        # A source that cannot be read will never be cut, so it gets an
        # expiry now; otherwise it would sit on disk forever.
        db.execute(
            """
            UPDATE jobs SET status = %s, error = %s, expires_at = %s,
                            claimed_by = NULL, claimed_at = NULL, updated_at = now()
            WHERE id = %s
            """,
            (FAILED, f"could not read the video: {exc}",
             _now() + timedelta(days=settings.clip_retention_days), job_id),
        )
        return

    db.execute(
        """
        UPDATE jobs SET duration_seconds = %s, width = %s, height = %s, fps = %s,
                        variable_frame_rate = %s, status = %s, updated_at = now()
        WHERE id = %s
        """,
        (info.duration, info.width, info.height, info.fps,
         info.variable_frame_rate, TRANSCRIBING, job_id),
    )
    log.info(
        "job %s probed: %.1fs, %dx%d, %.3ffps%s",
        job_id, info.duration, info.width, info.height, info.fps,
        ", variable frame rate" if info.variable_frame_rate else "",
    )

    # Transcription runs in the same claim: the worker already holds the job
    # and the source file is already on disk.
    transcribe_job(get_job(job_id))


# ------------------------------------------------------------ transcribe ----

def transcribe_job(job: dict[str, Any]) -> None:
    """Transcribe, suggest, and (if there are suggestions) queue the render.

    Neither transcription nor suggestion may fail the job. A video with no
    speech, or a model that is down, still leaves a usable tool: the operator
    lands on the review page with the reason shown and the manual tools live.
    """
    job_id = str(job["id"])
    store = storage()
    provider = get_transcription_provider()

    segments: list[TranscriptSegment] = []
    language = None
    transcript_error = None

    if not provider.enabled:
        transcript_error = "transcription is switched off on this server"
    else:
        source = store.path_for(job["source_path"])
        audio_key = f"sources/{job_id}/audio.wav"
        try:
            audio = extract_audio(
                source, store.path_for(audio_key), timeout=settings.ffmpeg_timeout_seconds
            )
            segments, language = provider.transcribe(audio)
        except (MediaError, TranscriptionError) as exc:
            transcript_error = str(exc)
            log.warning("job %s could not be transcribed: %s", job_id, exc)
        except Exception as exc:  # noqa: BLE001 - a provider may raise anything
            transcript_error = f"unexpected transcription error: {exc}"
            log.exception("job %s transcription crashed", job_id)
        finally:
            # The WAV is only an intermediate: up to 115 MB per hour, no value
            # once the text exists.
            store.delete(audio_key)

    if segments:
        _store_transcript(job_id, segments)

    db.execute(
        """
        UPDATE jobs SET transcript_language = %s, transcript_error = %s, updated_at = now()
        WHERE id = %s
        """,
        (language, transcript_error, job_id),
    )

    suggestions = _run_suggestions(job_id, segments) if segments else []

    if suggestions:
        # Precut: the whole point is that the operator opens a page of finished
        # clips, not a page of proposals.
        replace_clips(job_id, [
            ClipEdit(start=s.start, end=s.end, label=s.title) for s in suggestions
        ])
        set_status(job_id, QUEUED)
        log.info("job %s: %d suggested clips queued for render", job_id, len(suggestions))
    else:
        set_status(job_id, AWAITING_CUTS)


def _store_transcript(job_id: str, segments: Sequence[TranscriptSegment]) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM transcript_segments WHERE job_id = %s", (job_id,))
        for segment in segments:
            conn.execute(
                """
                INSERT INTO transcript_segments
                    (job_id, idx, start_seconds, end_seconds, text, words)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id, segment.index, segment.start, segment.end, segment.text,
                    Json([{"s": w.start, "e": w.end, "t": w.text} for w in segment.words]),
                ),
            )


def _run_suggestions(job_id: str, segments: Sequence[TranscriptSegment]) -> list:
    """Ask the model, record the outcome either way, return resolved picks."""
    provider = get_suggestion_provider()
    if not provider.enabled:
        db.execute(
            "UPDATE jobs SET suggestions = %s, suggestion_error = %s WHERE id = %s",
            (Json([]), "clip suggestions are switched off on this server", job_id),
        )
        return []
    try:
        suggestions = suggest_clips(segments, target_count=settings.suggestion_count, provider=provider)
        error = None
    except SuggestionError as exc:
        suggestions, error = [], str(exc)
        log.warning("job %s got no suggestions: %s", job_id, exc)
    except Exception as exc:  # noqa: BLE001
        suggestions, error = [], f"unexpected error: {exc}"
        log.exception("job %s suggestion step crashed", job_id)

    db.execute(
        "UPDATE jobs SET suggestions = %s, suggestion_error = %s, updated_at = now() WHERE id = %s",
        (Json(suggestions_to_json(suggestions)), error, job_id),
    )
    return suggestions


def request_suggestions(job_id: str) -> None:
    """Queue a fresh suggestion pass for the worker. Never runs in a request."""
    job = get_job(job_id)
    if job is None:
        raise ValueError("no such job")
    if job["status"] not in EDITABLE:
        raise ValueError(f"cannot re-run suggestions while the job is {job['status']}")
    db.execute(
        "UPDATE jobs SET status = %s, resume_status = %s, updated_at = now() WHERE id = %s",
        (SUGGEST_REQUESTED, job["status"], job_id),
    )


def suggest_job(job: dict[str, Any]) -> None:
    """Worker stage for a requested re-run. Refreshes the suggestion list only;
    it never touches clips the operator may have tweaked."""
    job_id = str(job["id"])
    _run_suggestions(job_id, transcript_segments_for(job_id))
    set_status(job_id, job.get("resume_status") or AWAITING_CUTS)


# ------------------------------------------------------------------ cuts ----

@dataclass(frozen=True)
class ClipEdit:
    """One row from the review page: an existing clip (id set) or a new one."""

    start: float
    end: float
    label: str | None = None
    id: int | None = None


def _same(a: float, b: float) -> bool:
    return abs(a - b) < 0.0005


def apply_clip_edits(job_id: str, edits: Sequence[ClipEdit]) -> dict[str, int]:
    """Reconcile the operator's rows with the stored clips.

    Clips missing from ``edits`` are deleted. Rows with an id whose timing or
    label changed are re-rendered; unchanged ones are left alone, so a tweak
    to one clip never re-encodes the other nine. Rows without an id that
    exactly match an existing clip are treated as that clip (this is what
    makes the text box a safe way to edit).

    Returns counts, and queues the job if anything needs rendering.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("no such job")
    if job["status"] not in EDITABLE:
        raise ValueError(f"clips cannot be edited while the job is {job['status']}")

    existing = {clip["id"]: clip for clip in get_clips(job_id)}
    store = storage()
    kept: set[int] = set()
    counts = {"unchanged": 0, "changed": 0, "added": 0, "deleted": 0}

    # Match id-less rows to existing clips by exact timing and label.
    def find_match(edit: ClipEdit) -> dict[str, Any] | None:
        for clip in existing.values():
            if clip["id"] in kept:
                continue
            if (_same(float(clip["start_seconds"]), edit.start)
                    and _same(float(clip["end_seconds"]), edit.end)
                    and (clip["label"] or None) == (edit.label or None)):
                return clip
        return None

    next_sequence = max((c["sequence"] for c in existing.values()), default=0) + 1
    total_after = len(edits)

    with db.connection() as conn:
        for edit in edits:
            clip = existing.get(edit.id) if edit.id is not None else find_match(edit)

            if clip is None:
                conn.execute(
                    """
                    INSERT INTO clips (job_id, sequence, label, start_seconds, end_seconds,
                                       output_filename, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (job_id, next_sequence, edit.label, edit.start, edit.end,
                     clip_filename(job["source_filename"], next_sequence, max(total_after, next_sequence), edit.label),
                     CLIP_PENDING),
                )
                next_sequence += 1
                counts["added"] += 1
                continue

            kept.add(clip["id"])
            unchanged = (
                _same(float(clip["start_seconds"]), edit.start)
                and _same(float(clip["end_seconds"]), edit.end)
                and (clip["label"] or None) == (edit.label or None)
                and clip["status"] == CLIP_COMPLETE
            )
            if unchanged:
                counts["unchanged"] += 1
                continue

            # Timing or label changed: the old file is wrong now.
            if clip["output_path"]:
                store.delete(clip["output_path"])
            conn.execute(
                """
                UPDATE clips SET start_seconds = %s, end_seconds = %s, label = %s,
                                 output_filename = %s, output_path = NULL, output_bytes = NULL,
                                 rendered_duration = NULL, status = %s, error = NULL
                WHERE id = %s
                """,
                (edit.start, edit.end, edit.label,
                 clip_filename(job["source_filename"], clip["sequence"], max(total_after, clip["sequence"]), edit.label),
                 CLIP_PENDING, clip["id"]),
            )
            counts["changed"] += 1

        for clip_id, clip in existing.items():
            if clip_id not in kept:
                if clip["output_path"]:
                    store.delete(clip["output_path"])
                conn.execute("DELETE FROM clips WHERE id = %s", (clip_id,))
                counts["deleted"] += 1

    if counts["added"] or counts["changed"]:
        set_status(job_id, QUEUED)
    else:
        settle_status(job_id)

    log.info("job %s edits applied: %s", job_id, counts)
    return counts


def replace_clips(job_id: str, edits: Sequence[ClipEdit]) -> None:
    """Throw away every clip and start again from ``edits``. Used by the
    automatic precut and by tests; the review page uses apply_clip_edits."""
    store = storage()
    job = get_job(job_id)
    with db.connection() as conn:
        for clip in get_clips(job_id):
            if clip["output_path"]:
                store.delete(clip["output_path"])
        conn.execute("DELETE FROM clips WHERE job_id = %s", (job_id,))
        total = len(edits)
        for sequence, edit in enumerate(edits, start=1):
            conn.execute(
                """
                INSERT INTO clips (job_id, sequence, label, start_seconds, end_seconds,
                                   output_filename, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (job_id, sequence, edit.label, edit.start, edit.end,
                 clip_filename(job["source_filename"], sequence, total, edit.label),
                 CLIP_PENDING),
            )


def set_cuts(job_id: str, ranges: Sequence[CutRange]) -> None:
    """Parsed cut list -> clips -> queued. Kept for the text-box path and tests."""
    replace_clips(job_id, [ClipEdit(start=r.start, end=r.end, label=r.label) for r in ranges])
    set_status(job_id, QUEUED)


def settle_status(job_id: str) -> None:
    """Derive the job's status from its clips after a non-rendering change."""
    clips = get_clips(job_id)
    if not clips:
        set_status(job_id, AWAITING_CUTS)
    elif any(c["status"] in (CLIP_PENDING, CLIP_RENDERING) for c in clips):
        set_status(job_id, QUEUED)
    elif any(c["status"] == CLIP_FAILED for c in clips):
        failed = sum(1 for c in clips if c["status"] == CLIP_FAILED)
        set_status(job_id, FAILED, f"{failed} of {len(clips)} clips failed to render")
    else:
        set_status(job_id, COMPLETE)


# ---------------------------------------------------------------- render ----

def render_job(job: dict[str, Any]) -> None:
    """Render every clip that is not already rendered.

    One clip failing does not abandon the rest: each is rendered and recorded
    independently, and the job is marked failed at the end if any did not make
    it, with the source kept so it can be fixed from the review page.
    """
    job_id = str(job["id"])
    store = storage()
    source = store.path_for(job["source_path"])
    clips = get_clips(job_id)
    failures = 0
    rendered = 0

    for clip in clips:
        if clip["status"] == CLIP_COMPLETE and clip["output_path"] and store.exists(clip["output_path"]):
            continue  # untouched since last render; never re-encode it

        clip_id = clip["id"]
        destination_key = f"clips/{job_id}/{clip['output_filename']}"
        destination = store.path_for(destination_key)
        duration = float(clip["end_seconds"]) - float(clip["start_seconds"])

        db.execute("UPDATE clips SET status = %s WHERE id = %s", (CLIP_RENDERING, clip_id))
        _touch(job_id)

        try:
            info = cut_clip(
                source, destination,
                float(clip["start_seconds"]), duration,
                source_fps=float(job["fps"]) if job["fps"] else None,
                timeout=settings.ffmpeg_timeout_seconds,
            )
        except (MediaError, CutVerificationError, ValueError) as exc:
            failures += 1
            log.error("job %s clip %s failed: %s", job_id, clip["sequence"], exc)
            db.execute(
                "UPDATE clips SET status = %s, error = %s WHERE id = %s",
                (CLIP_FAILED, str(exc), clip_id),
            )
            continue

        rendered += 1
        db.execute(
            """
            UPDATE clips SET status = %s, output_path = %s, output_bytes = %s,
                             rendered_duration = %s, error = NULL
            WHERE id = %s
            """,
            (CLIP_COMPLETE, destination_key, destination.stat().st_size, info.duration, clip_id),
        )

    expires_at = _now() + timedelta(days=settings.clip_retention_days)
    db.execute("UPDATE jobs SET expires_at = %s WHERE id = %s", (expires_at, job_id))
    settle_status(job_id)
    log.info("job %s: rendered %d, failed %d, skipped %d already done",
             job_id, rendered, failures, len(clips) - rendered - failures)


# -------------------------------------------------------------- finalise ----

def finalise_job(job_id: str) -> dict[str, Any]:
    """Lock the job: renumber clips contiguously, rename files, drop the source.

    Deletions during review leave gaps (01, 03, 04). The deliverable is
    clip_01..N with no holes, so numbering is settled here, once, when the
    operator says they are done. The source is deleted now because nothing
    can be re-cut after this.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("no such job")
    if job["status"] != COMPLETE:
        raise ValueError(f"only a complete job can be finalised (this one is {job['status']})")

    store = storage()
    clips = [c for c in get_clips(job_id) if c["status"] == CLIP_COMPLETE]
    total = len(clips)

    with db.connection() as conn:
        # Two passes: park every sequence out of the way first, so renumbering
        # never collides with the UNIQUE (job_id, sequence) constraint.
        for clip in clips:
            conn.execute("UPDATE clips SET sequence = sequence + 100000 WHERE id = %s", (clip["id"],))
        for sequence, clip in enumerate(clips, start=1):
            new_name = clip_filename(job["source_filename"], sequence, total, clip["label"])
            new_key = f"clips/{job_id}/{new_name}"
            if clip["output_path"] and clip["output_path"] != new_key:
                store.move(clip["output_path"], new_key)
            conn.execute(
                "UPDATE clips SET sequence = %s, output_filename = %s, output_path = %s WHERE id = %s",
                (sequence, new_name, new_key, clip["id"]),
            )
        conn.execute(
            """
            UPDATE jobs SET status = %s, finalised_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (FINALISED, job_id),
        )

    delete_source(job_id)
    log.info("job %s finalised with %d clips", job_id, total)
    return get_job(job_id)


def delete_source(job_id: str) -> None:
    job = get_job(job_id)
    if job is None or not job["source_path"] or job["source_deleted_at"]:
        return
    freed = storage().size(job["source_path"])
    storage().delete(f"sources/{job_id}")
    db.execute(
        "UPDATE jobs SET source_deleted_at = now(), updated_at = now() WHERE id = %s",
        (job_id,),
    )
    log.info("job %s source deleted, freed %.1f MB", job_id, freed / 1024**2)


# -------------------------------------------------------------- manifest ----

def build_manifest(job: dict[str, Any]) -> dict[str, Any]:
    clips = get_clips(str(job["id"]))
    return {
        "job_id": str(job["id"]),
        "source_filename": job["source_filename"],
        "source_duration_seconds": job["duration_seconds"],
        "source_resolution": f"{job['width']}x{job['height']}" if job["width"] else None,
        "source_fps": job["fps"],
        "transcript_language": job.get("transcript_language"),
        "rendered_at": job["updated_at"].isoformat() if job["updated_at"] else None,
        "finalised_at": job["finalised_at"].isoformat() if job.get("finalised_at") else None,
        "clips_expire_at": job["expires_at"].isoformat() if job["expires_at"] else None,
        "clip_count": len(clips),
        "clips": [
            {
                "sequence": clip["sequence"],
                "filename": clip["output_filename"],
                "label": clip["label"],
                "start": format_timestamp(float(clip["start_seconds"])),
                "end": format_timestamp(float(clip["end_seconds"])),
                "start_seconds": round(float(clip["start_seconds"]), 3),
                "end_seconds": round(float(clip["end_seconds"]), 3),
                "requested_duration_seconds": round(
                    float(clip["end_seconds"]) - float(clip["start_seconds"]), 3),
                "rendered_duration_seconds": (
                    round(float(clip["rendered_duration"]), 3)
                    if clip["rendered_duration"] else None),
                "bytes": clip["output_bytes"],
                "status": clip["status"],
                "error": clip["error"],
            }
            for clip in clips
        ],
    }


def manifest_bytes(job: dict[str, Any]) -> bytes:
    return json.dumps(build_manifest(job), indent=2).encode()


# ------------------------------------------------------------- retention ----

def purge_expired() -> int:
    """Delete files for jobs past their retention date. The job row stays, as
    history, marked expired."""
    expired = db.query(
        """
        SELECT id FROM jobs
        WHERE expires_at IS NOT NULL AND expires_at < now() AND status <> %s
        """,
        (EXPIRED,),
    )
    store = storage()
    for row in expired:
        job_id = str(row["id"])
        store.delete(f"clips/{job_id}")
        store.delete(f"sources/{job_id}")
        with db.connection() as conn:
            conn.execute(
                "UPDATE clips SET output_path = NULL, output_bytes = NULL WHERE job_id = %s",
                (job_id,),
            )
            conn.execute(
                """
                UPDATE jobs SET status = %s, source_deleted_at = COALESCE(source_deleted_at, now()),
                                updated_at = now()
                WHERE id = %s
                """,
                (EXPIRED, job_id),
            )
        log.info("job %s expired; files purged", job_id)
    return len(expired)


def abandoned_uploads(older_than_hours: int = 24) -> Iterator[str]:
    """Uploads that were started and never finished."""
    for row in db.query(
        """
        SELECT id FROM jobs
        WHERE status = %s AND updated_at < now() - make_interval(hours => %s)
        """,
        (UPLOADING, older_than_hours),
    ):
        yield str(row["id"])
