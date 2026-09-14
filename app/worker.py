"""Background worker.

Runs as its own process alongside the web app, sharing the database and the
data volume. It exists because probing and rendering take minutes of sustained
CPU, and no HTTP request should ever wait on that.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from datetime import datetime, timezone

from app import auth, db, jobs
from app.config import settings

log = logging.getLogger(__name__)

# Housekeeping runs on a timer rather than a cron container: one less moving
# part, and the worker is already awake.
HOUSEKEEPING_INTERVAL_SECONDS = 3600

_shutdown = False


def _request_shutdown(signum, _frame) -> None:
    global _shutdown
    log.info("signal %s received, finishing current job then stopping", signum)
    _shutdown = True


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def housekeeping() -> None:
    """Retention and cleanup. Failures here must never stop the worker."""
    try:
        released = jobs.release_stale_claims()
        if released:
            log.warning("returned %d stale job(s) to the queue", released)

        purged = jobs.purge_expired()
        if purged:
            log.info("purged %d job(s) past retention", purged)

        for job_id in jobs.abandoned_uploads():
            jobs.storage().delete(f"sources/{job_id}")
            jobs.set_status(job_id, jobs.FAILED, "upload was never completed")
            log.info("cleaned up abandoned upload %s", job_id)

        tokens = auth.purge_expired_tokens()
        if tokens:
            log.info("purged %d expired login token(s)", tokens)
    except Exception:  # noqa: BLE001 - housekeeping must not kill the loop
        log.exception("housekeeping failed; continuing")


def process(job: dict) -> None:
    job_id = str(job["id"])
    status = job["status"]
    started = time.monotonic()

    try:
        if status == jobs.PROBING:
            jobs.probe_job(job)
        elif status == jobs.RENDERING:
            jobs.render_job(job)
        elif status == jobs.SUGGESTING:
            jobs.suggest_job(job)
        elif status == jobs.EDITING:
            jobs.edit_job(job)
        else:  # pragma: no cover - claim_next_job only produces the four above
            log.error("job %s claimed in unexpected status %s", job_id, status)
            jobs.set_status(job_id, jobs.FAILED, f"unexpected status {status}")
    except Exception as exc:  # noqa: BLE001
        # A crash mid-render must leave the job in a state the operator can
        # see and retry, never silently stuck as "rendering".
        log.exception("job %s crashed", job_id)
        jobs.set_status(job_id, jobs.FAILED, f"unexpected error: {exc}")
    finally:
        log.info("job %s handled in %.1fs", job_id, time.monotonic() - started)


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    settings.ensure_dirs()
    db.wait_for_database()
    db.apply_schema()

    me = worker_id()
    log.info("worker %s started, polling every %ss", me, settings.worker_poll_seconds)

    last_housekeeping = 0.0
    while not _shutdown:
        now = time.monotonic()
        if now - last_housekeeping > HOUSEKEEPING_INTERVAL_SECONDS:
            housekeeping()
            last_housekeeping = now

        try:
            job = jobs.claim_next_job(me)
        except Exception:  # noqa: BLE001 - a database blip must not end the worker
            log.exception("could not claim a job; retrying")
            time.sleep(settings.worker_poll_seconds)
            continue

        if job is None:
            time.sleep(settings.worker_poll_seconds)
            continue

        log.info(
            "claimed job %s (%s)", str(job["id"])[:8], job["source_filename"]
        )
        process(job)

    log.info("worker %s stopped", me)


if __name__ == "__main__":
    run()
