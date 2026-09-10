"""Transcription, behind a swappable provider interface.

The transcript is what the whole product stands on: it is how the AI knows
what was said and exactly when, and how the operator reads a video instead of
scrubbing it. It supplies every timestamp the system uses. Nothing here decides
what to cut; that is the suggester's job, and it may only choose segments that
exist in here.

The interface exists because the provider is expected to change. Self-hosted
faster-whisper costs nothing per video and keeps client footage on the machine;
a hosted API is faster and adds speaker labels. Swapping is one class.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptSegment:
    """One spoken chunk, with the timings the model actually measured.

    ``index`` is the stable handle used everywhere else: the UI links to it and
    the suggester returns it instead of raw seconds, so a timestamp can never be
    invented.
    """

    index: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


class TranscriptionError(RuntimeError):
    """Transcription could not be completed."""


class TranscriptionProvider(ABC):
    name: str = "abstract"
    # False means "do not even extract audio": the pipeline skips straight to
    # the manual tools instead of spending minutes decoding a file for nothing.
    enabled: bool = True

    @abstractmethod
    def transcribe(self, audio_path: Path) -> tuple[list[TranscriptSegment], str]:
        """Return the segments and the detected language code."""


class FasterWhisperProvider(TranscriptionProvider):
    """Local transcription with faster-whisper (CTranslate2).

    No per-video cost and nothing leaves the machine. On two ARM cores the
    expectation, not yet a measurement, is roughly the length of the video;
    the first real run on the VM produces the number.
    """

    name = "faster-whisper"

    def __init__(self):
        self.model_size = settings.whisper_model_size
        self.device = settings.whisper_device
        self.compute_type = settings.whisper_compute_type
        self.timeout = settings.transcription_timeout_seconds
        self._model = None

    def _load(self):
        """Load the model on first use, not at import.

        Loading at import would block the worker from starting until the
        weights are on disk. Lazy loading keeps startup fast and a missing
        model a clear error on the job rather than a crashed process.
        """
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - depends on install
                raise TranscriptionError(
                    "faster-whisper is not installed; add it to requirements.txt "
                    "or set TRANSCRIPTION_PROVIDER=disabled"
                ) from exc

            log.info(
                "loading whisper model %s (%s, %s) from %s",
                self.model_size, self.device, self.compute_type, settings.models_dir,
            )
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(settings.models_dir),
            )
        return self._model

    def transcribe(self, audio_path: Path) -> tuple[list[TranscriptSegment], str]:
        model = self._load()
        started = time.monotonic()
        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                beam_size=5,
                # Word timings are what let a cut land on a word boundary
                # rather than mid-syllable. This is the whole point.
                word_timestamps=True,
                # Voice activity detection. Without it Whisper reliably
                # hallucinates text during silence, and a hallucinated line
                # would put a cut point where nobody spoke.
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )

            segments: list[TranscriptSegment] = []
            # faster-whisper yields segments lazily; the work happens inside
            # this loop, which is why the deadline is checked per segment.
            for segment in raw_segments:
                if time.monotonic() - started > self.timeout:
                    raise TranscriptionError(
                        f"transcription exceeded {self.timeout}s; the video may "
                        "be too long for this machine, or the model too large"
                    )
                text = (segment.text or "").strip()
                if not text:
                    continue
                words = [
                    Word(start=round(w.start, 3), end=round(w.end, 3), text=w.word.strip())
                    for w in (segment.words or [])
                    if w.word and w.word.strip()
                ]
                segments.append(
                    TranscriptSegment(
                        index=len(segments),
                        start=round(segment.start, 3),
                        end=round(segment.end, 3),
                        text=text,
                        words=words,
                    )
                )
        except TranscriptionError:
            raise
        except Exception as exc:  # noqa: BLE001 - the library raises broadly
            raise TranscriptionError(f"transcription failed: {exc}") from exc

        if not segments:
            raise TranscriptionError(
                "no speech was found in this video; it may be silent or music only"
            )

        language = getattr(info, "language", None) or "unknown"
        log.info(
            "transcribed %d segments (%s) in %.0fs with %s",
            len(segments), language, time.monotonic() - started, self.name,
        )
        return segments, language


class NullTranscriptionProvider(TranscriptionProvider):
    """Transcription switched off. The pipeline skips audio extraction entirely."""

    name = "disabled"
    enabled = False

    def transcribe(self, audio_path: Path) -> tuple[list[TranscriptSegment], str]:
        raise TranscriptionError("transcription is disabled")


_PROVIDERS: dict[str, type[TranscriptionProvider]] = {
    "faster-whisper": FasterWhisperProvider,
    "disabled": NullTranscriptionProvider,
}

_instance: TranscriptionProvider | None = None


def get_provider() -> TranscriptionProvider:
    """The configured provider, built once and reused.

    Reused because loading Whisper weights takes seconds and the worker
    transcribes many videos over its life.
    """
    global _instance
    if _instance is None:
        provider_class = _PROVIDERS.get(settings.transcription_provider)
        if provider_class is None:
            raise TranscriptionError(
                f"unknown transcription provider {settings.transcription_provider!r}; "
                f"choose one of {', '.join(sorted(_PROVIDERS))}"
            )
        _instance = provider_class()
    return _instance


def set_provider(provider: TranscriptionProvider | None) -> None:
    """Override the provider. Used by tests to avoid loading real weights."""
    global _instance
    _instance = provider
