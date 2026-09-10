"""Configuration, read once from the environment at import."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else int(raw)


def _csv(name: str) -> list[str]:
    return [item.strip().lower() for item in os.getenv(name, "").split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://clipstory:clipstory@localhost:5432/clipstory"
    )
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))

    # Signs login tokens and session cookies. Generated per deploy; changing it
    # logs everyone out, which is the intended way to revoke all sessions.
    secret_key: str = os.getenv("SECRET_KEY", "")

    base_url: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

    # Seeded into the invite list and given the admin flag on first start, so a
    # fresh deployment has someone who can invite everyone else.
    admin_emails: list[str] = field(default_factory=lambda: _csv("ADMIN_EMAILS"))

    # Email delivery. Without an API key the app logs magic links instead of
    # sending them, which is what makes first-run testing possible before any
    # email provider is configured.
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    mail_from: str = os.getenv("MAIL_FROM", "Clipstory <onboarding@resend.dev>")

    login_token_ttl_minutes: int = _int("LOGIN_TOKEN_TTL_MINUTES", 20)
    session_ttl_days: int = _int("SESSION_TTL_DAYS", 30)

    # Retention, per the agreed policy: the source is the expensive object and
    # is dropped as soon as its clips render. A failed job keeps its source so
    # it can be retried without re-uploading.
    clip_retention_days: int = _int("CLIP_RETENTION_DAYS", 30)
    delete_source_after_render: bool = _bool("DELETE_SOURCE_AFTER_RENDER", True)

    max_upload_bytes: int = _int("MAX_UPLOAD_BYTES", 8 * 1024**3)
    upload_chunk_bytes: int = _int("UPLOAD_CHUNK_BYTES", 8 * 1024**2)

    # Renders per job run one after another. At the agreed 5 to 10 clips a job
    # finishes in minutes, and sequential keeps memory and CPU predictable on a
    # 2-core box. Raise only with measured timings in hand.
    render_concurrency: int = _int("RENDER_CONCURRENCY", 1)
    worker_poll_seconds: int = _int("WORKER_POLL_SECONDS", 3)
    ffmpeg_timeout_seconds: int = _int("FFMPEG_TIMEOUT_SECONDS", 3600)

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for directory in (self.sources_dir, self.clips_dir, self.uploads_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Return configuration problems worth refusing to start over."""
        problems = []
        if not self.secret_key:
            problems.append("SECRET_KEY is not set; sessions cannot be signed")
        elif len(self.secret_key) < 32:
            problems.append("SECRET_KEY is shorter than 32 characters")
        if not self.admin_emails:
            problems.append("ADMIN_EMAILS is empty; nobody would be able to log in")
        return problems


settings = Settings()
