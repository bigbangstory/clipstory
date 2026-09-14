"""Configuration, read once from the environment at import.

This is the only module that reads environment variables for application
settings. Everything else imports ``settings``. Keeping one reader means one
place to document a key and no two defaults that agree by coincidence.
"""
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


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else float(raw)


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

    # Seeded into the invite list and given the admin flag on first sign-in,
    # so a fresh deployment has someone who can invite everyone else.
    admin_emails: list[str] = field(default_factory=lambda: _csv("ADMIN_EMAILS"))

    # Email delivery. Without an API key the app logs magic links instead of
    # sending them, which is what makes first-run testing possible before any
    # email provider is configured.
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    mail_from: str = os.getenv("MAIL_FROM", "Clipstory <onboarding@resend.dev>")

    login_token_ttl_minutes: int = _int("LOGIN_TOKEN_TTL_MINUTES", 20)
    session_ttl_days: int = _int("SESSION_TTL_DAYS", 30)

    # Retention. The source video stays until the operator finalises the job,
    # because tweaking a clip needs it. Finalising deletes the source; clips
    # and the job itself are purged this many days after rendering.
    clip_retention_days: int = _int("CLIP_RETENTION_DAYS", 30)

    # --- transcription -----------------------------------------------------
    # "faster-whisper" runs on this machine at no per-video cost; "disabled"
    # skips straight to the manual cut tools.
    transcription_provider: str = os.getenv("TRANSCRIPTION_PROVIDER", "faster-whisper")
    whisper_model_size: str = os.getenv("WHISPER_MODEL_SIZE", "base")
    whisper_device: str = os.getenv("WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    # Whisper on two ARM cores runs at roughly real time, so an hour of video
    # needs about an hour. Three hours leaves room for a slow box.
    transcription_timeout_seconds: int = _int("TRANSCRIPTION_TIMEOUT_SECONDS", 3 * 3600)

    # --- clip suggestions --------------------------------------------------
    # "ollama" is a local model on this machine (default, free). "anthropic"
    # is the hosted Claude API and needs ANTHROPIC_API_KEY. "disabled" turns
    # the feature off. Whichever is used, the model returns transcript segment
    # numbers and never a timestamp.
    suggest_provider: str = os.getenv("SUGGEST_PROVIDER", "ollama")
    # Empty means the provider's own default: qwen2.5:7b-instruct for Ollama,
    # claude-opus-5 for Anthropic.
    suggest_model: str = os.getenv("SUGGEST_MODEL", "")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
    # Context window given to the local model. A one-hour transcript is about
    # 13k tokens; transcripts longer than fit are processed in windows. Larger
    # values cost RAM for the KV cache.
    ollama_num_ctx: int = _int("OLLAMA_NUM_CTX", 16384)
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    suggestion_count: int = _int("SUGGESTION_COUNT", 8)
    suggest_timeout_seconds: int = _int("SUGGEST_TIMEOUT_SECONDS", 1800)

    # --- text-based editing -------------------------------------------------
    # Disfluencies removed by the cleanup button. Anything conversational
    # ("so", "like", "right") is deliberately absent: each is usually doing
    # real work in a sentence, and a cleanup button should not silently change
    # what someone said. app.edits.OPTIONAL_FILLERS holds those for opt-in use.
    filler_words: list[str] = field(
        default_factory=lambda: _csv("FILLER_WORDS") or None
    )
    # Pauses longer than the threshold are shortened to the target, not
    # removed: speech with its pauses stripped sounds rushed.
    silence_threshold_seconds: float = _float("SILENCE_THRESHOLD_SECONDS", 0.8)
    silence_target_seconds: float = _float("SILENCE_TARGET_SECONDS", 0.4)
    # A full-length export re-encodes the whole video, unlike a clip. Generous,
    # because on two ARM cores this is the slowest thing the worker does.
    edit_timeout_seconds: int = _int("EDIT_TIMEOUT_SECONDS", 4 * 3600)

    max_upload_bytes: int = _int("MAX_UPLOAD_BYTES", 8 * 1024**3)
    upload_chunk_bytes: int = _int("UPLOAD_CHUNK_BYTES", 8 * 1024**2)

    worker_poll_seconds: int = _int("WORKER_POLL_SECONDS", 3)
    ffmpeg_timeout_seconds: int = _int("FFMPEG_TIMEOUT_SECONDS", 3600)

    @property
    def suggestions_enabled(self) -> bool:
        if self.suggest_provider == "disabled":
            return False
        if self.suggest_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return True

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def models_dir(self) -> Path:
        """Whisper weights. On the data volume so a rebuild does not refetch."""
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for directory in (self.sources_dir, self.clips_dir, self.models_dir):
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
        if self.transcription_provider not in {"faster-whisper", "disabled"}:
            problems.append(f"TRANSCRIPTION_PROVIDER={self.transcription_provider!r} is not one of faster-whisper, disabled")
        if self.suggest_provider not in {"ollama", "anthropic", "disabled"}:
            problems.append(f"SUGGEST_PROVIDER={self.suggest_provider!r} is not one of ollama, anthropic, disabled")
        if self.suggest_provider == "anthropic" and not self.anthropic_api_key:
            problems.append("SUGGEST_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
        return problems


settings = Settings()
