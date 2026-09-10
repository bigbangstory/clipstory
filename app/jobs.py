"""Job lifecycle: upload, probe, cut definition, render, retention.

Status flow::

    uploading -> uploaded -> probing -> awaiting_cuts -> queued
                                                          |
                                                          v
                                               rendering -> complete
                                                          -> failed

Everything that takes real time (probing, rendering) happens in the worker, so
no HTTP request ever waits on ffmpeg. State lives in Postgres, so a container
restart resumes rather than losing the job.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app import db
from app.config import settings
from app.media import CutVerificationError, MediaError, cut_clip, extract_audio, probe
from app.naming import clip_filename
from psycopg.types.json import Json

from app.storage import LocalDiskStorage, Storage
from app.suggest import SuggestionError, suggest_clips
from app.transcription import (
    TranscriptSegment,
    TranscriptionError,
    get_provider as get_transcription_provider,
)
from app.timestamps import CutRange, format_timestamp

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

# Statuses the worker will pick up. PROBING covers the probe-plus-transcribe
# stage; QUEUED covers rendering.
CLAIMABLE = (UPLOADED, QUEUED)

# A job left mid-flight by a crashed or restarted worker is returned to the
# queue after this long. Generous, because a legitimate long render must not be
# stolen by a second worker while it is still going.
STALE_CLAIM_MINUTES = 180


def storage() -> Storage:
    return LocalDiskStorage(settings.data_dir)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _touch(job_id: str) -> None:
    db.execute("UPDATE jobs SET updated_at = now() WHERE id = %s", (job_id,))


def set_status(job_id: str, status: str, error: str | None = None) -> None:
    db.execute(
        "UPDATE jobs SET status = %s, error = %s, updated_at = now() WHERE id = %s",
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

    store = storage()
    with store.open_write(job["source_path"], append=True) as handle:
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

    store = storage()
    on_disk = store.size(job["source_path"])
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


# ------------------------------------------------------------------ cuts ----

def set_cuts(job_id: str, ranges: list[CutRange]) -> None:
    """Replace the job's cut list and queue it for rendering.

    Replacing rather than appending means a re-submission after a failure
    starts clean instead of leaving orphaned clip rows behind.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError("no such job")

    total = len(ranges)
    with db.connection() as conn:
        conn.execute("DELETE FROM clips WHERE job_id = %s", (job_id,))
        for cut in ranges:
            conn.execute(
                """
                INSERT INTO clips
                    (job_id, sequence, label, start_seconds, end_seconds, output_filename)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    cut.sequence,
                    cut.label,
                    cut.start,
                    cut.end,
                    clip_filename(job["source_filename"], cut.sequence, total, cut.label),
                ),
            )
        conn.execute(
            "UPDATE jobs SET status = %s, error = NULL, updated_at = now() WHERE id = %s",
            (QUEUED, job_id),
        )
    log.info("job %s queued with %d clips", job_id, total)


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
            status = CASE WHEN status = %s THEN %s ELSE %s END,
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
        (UPLOADED, PROBING, RENDERING, worker_id, list(CLAIMABLE)),
    )


def release_stale_claims() -> int:
    """Return jobs abandoned by a dead worker to the queue."""
    with db.connection() as conn:
        cursor = conn.execute(
            """
            UPDATE jobs SET
                status = CASE WHEN status = %s THEN %s ELSE %s END,
                claimed_by = NULL, claimed_at = NULL, updated_at = now()
            WHERE status IN (%s, %s)
              AND claimed_at < now() - make_interval(mins => %s)
            """,
            (PROBING, UPLOADED, QUEUED, PROBING, RENDERING, STALE_CLAIM_MINUTES),
        )
        return cursor.rowcount or 0


# ----------------------------------------------------------------- probe ----

def probe_job(job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    store = storage()
    source = store.path_for(job["source_path"])
    try:
        info = probe(source)
    except MediaError as exc:
        log.error("job %s failed to probe: %s", job_id, exc)
        set_status(job_id, FAILED, f"could not read the video: {exc}")
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


# ----------------------------------------------------------- transcribe ----

def transcribe_job(job: dict[str, Any]) -> None:
    """Transcribe the source, then ask for clip suggestions.

    Neither is allowed to fail the job. A video with no speech, or a missing
    API key, still leaves a perfectly usable tool: the operator types their own
    timestamps as before. The reason is recorded and shown, not swallowed.
    """
    job_id = str(job["id"])
    store = storage()
    source = store.path_for(job["source_path"])
    audio_key = f"sources/{job_id}/audio.wav"

    segments: list = []
    language = None
    transcript_error = None

    try:
        audio = extract_audio(
            source, store.path_for(audio_key), timeout=settings.ffmpeg_timeout_seconds
        )
        segments, language = get_transcription_provider().transcribe(audio)
    except (MediaError, TranscriptionError) as exc:
        transcript_error = str(exc)
        log.warning("job %s could not be transcribed: %s", job_id, exc)
    except Exception as exc:  # noqa: BLE001 - a provider may raise anything
        transcript_error = f"unexpected transcription error: {exc}"
        log.exception("job %s transcription crashed", job_id)
    finally:
        # The WAV is only an intermediate. It is up to 115 MB per hour and has
        # no value once the text exists.
        store.delete(audio_key)

    if segments:
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
                        job_id,
                        segment.index,
                        segment.start,
                        segment.end,
                        segment.text,
                        Json([{"s": w.start, "e": w.end, "t": w.text} for w in segment.words]),
                    ),
                )

    suggestions_payload = None
    suggestion_error = None
    if segments and settings.suggestions_enabled:
        try:
            suggestions = suggest_clips(segments, target_count=settings.suggestion_count)
            suggestions_payload = Json([
                {
                    "start": s.start,
                    "end": s.end,
                    "title": s.title,
                    "reason": s.reason,
                    "start_segment": s.start_segment,
                    "end_segment": s.end_segment,
                }
                for s in suggestions
            ])
        except SuggestionError as exc:
            suggestion_error = str(exc)
            log.warning("job %s got no suggestions: %s", job_id, exc)
        except Exception as exc:  # noqa: BLE001
            suggestion_error = f"unexpected error: {exc}"
            log.exception("job %s suggestion step crashed", job_id)

    db.execute(
        """
        UPDATE jobs SET status = %s, transcript_language = %s, transcript_error = %s,
                        suggestions = %s, suggestion_error = %s,
                        claimed_by = NULL, claimed_at = NULL, updated_at = now()
        WHERE id = %s
        """,
        (AWAITING_CUTS, language, transcript_error,
         suggestions_payload, suggestion_error, job_id),
    )


def save_suggestions(job_id: str, suggestions, error: str | None) -> None:
    db.execute(
        """
        UPDATE jobs SET suggestions = %s, suggestion_error = %s, updated_at = now()
        WHERE id = %s
        """,
        (
            Json([
                {
                    "start": s.start, "end": s.end, "title": s.title,
                    "reason": s.reason, "start_segment": s.start_segment,
                    "end_segment": s.end_segment,
                }
                for s in suggestions
            ]) if suggestions else Json([]),
            error,
            job_id,
        ),
    )


def get_transcript(job_id: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT idx, start_seconds, end_seconds, text
        FROM transcript_segments WHERE job_id = %s ORDER BY idx
        """,
        (job_id,),
    )


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


# ---------------------------------------------------------------- render ----

def render_job(job: dict[str, Any]) -> None:
    """Render every clip in a job, then apply the retention rule.

    One clip failing does not abandon the rest: each is rendered and recorded
    independently, and the job is marked failed at the end if any did not make
    it. That way a single awkward range does not cost the operator the other
    nine clips.
    """
    job_id = str(job["id"])
    store = storage()
    source = store.path_for(job["source_path"])
    clips = get_clips(job_id)
    failures = 0

    for clip in clips:
        clip_id = clip["id"]
        destination_key = f"clips/{job_id}/{clip['output_filename']}"
        destination = store.path_for(destination_key)
        duration = float(clip["end_seconds"]) - float(clip["start_seconds"])

        db.execute("UPDATE clips SET status = 'rendering' WHERE id = %s", (clip_id,))
        _touch(job_id)

        try:
            info = cut_clip(
                source,
                destination,
                float(clip["start_seconds"]),
                duration,
                source_fps=float(job["fps"]) if job["fps"] else None,
                timeout=settings.ffmpeg_timeout_seconds,
            )
        except (MediaError, CutVerificationError, ValueError) as exc:
            failures += 1
            log.error("job %s clip %s failed: %s", job_id, clip["sequence"], exc)
            db.execute(
                "UPDATE clips SET status = 'failed', error = %s WHERE id = %s",
                (str(exc), clip_id),
            )
            continue

        db.execute(
            """
            UPDATE clips SET status = 'complete', output_path = %s, output_bytes = %s,
                             rendered_duration = %s, error = NULL
            WHERE id = %s
            """,
            (destination_key, destination.stat().st_size, info.duration, clip_id),
        )

    expires_at = _now() + timedelta(days=settings.clip_retention_days)

    if failures:
        db.execute(
            """
            UPDATE jobs SET status = %s, error = %s, expires_at = %s,
                            claimed_by = NULL, claimed_at = NULL, updated_at = now()
            WHERE id = %s
            """,
            (FAILED, f"{failures} of {len(clips)} clips failed to render",
             expires_at, job_id),
        )
        # The source is deliberately kept on failure so the job can be retried
        # without asking the operator to upload gigabytes again.
        log.warning("job %s finished with %d failures; source kept for retry", job_id, failures)
        return

    db.execute(
        """
        UPDATE jobs SET status = %s, error = NULL, expires_at = %s,
                        claimed_by = NULL, claimed_at = NULL, updated_at = now()
        WHERE id = %s
        """,
        (COMPLETE, expires_at, job_id),
    )
    log.info("job %s rendered %d clips", job_id, len(clips))

    if settings.delete_source_after_render:
        delete_source(job_id)


def delete_source(job_id: str) -> None:
    """Drop the source video. It is the expensive object and is rarely needed
    twice once its clips exist."""
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
        "source_resolution": (
            f"{job['width']}x{job['height']}" if job["width"] else None
        ),
        "source_fps": job["fps"],
        "rendered_at": job["updated_at"].isoformat() if job["updated_at"] else None,
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
                    float(clip["end_seconds"]) - float(clip["start_seconds"]), 3
                ),
                "rendered_duration_seconds": (
                    round(float(clip["rendered_duration"]), 3)
                    if clip["rendered_duration"] else None
                ),
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
    """Delete clips past their retention date. Returns jobs affected."""
    expired = db.query(
        "SELECT id FROM jobs WHERE expires_at IS NOT NULL AND expires_at < now()"
    )
    store = storage()
    for row in expired:
        job_id = str(row["id"])
        store.delete(f"clips/{job_id}")
        store.delete(f"sources/{job_id}")
        db.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        log.info("job %s purged after retention period", job_id)
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
