"""Transcription, behind a swappable provider interface.

The transcript is what makes the tool usable: without it you scrub a one-hour
video by hand to find your cut points. It is a reading aid and a source of real
timestamps, never a decision-maker. Nothing here chooses what to cut.

The interface exists because the provider is expected to change. Self-hosted
faster-whisper costs nothing per video and keeps client footage on your own
machine; a hosted API is faster and gives speaker labels. Swapping is one
class, with no change to the pipeline.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

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
    the LLM suggester returns it instead of raw seconds, so a timestamp can
    never be invented.
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

    @abstractmethod
    def transcribe(self, audio_path: Path) -> tuple[list[TranscriptSegment], str]:
        """Return the segments and the detected language code."""


class FasterWhisperProvider(TranscriptionProvider):
    """Local transcription with faster-whisper (CTranslate2).

    No per-video cost and nothing leaves the machine, which matters when the
    footage is a client's. On two ARM cores expect roughly the length of the
    video; that is acceptable because the job already runs in the background
    and the operator is not sitting waiting on a request.
    """

    name = "faster-whisper"

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ):
        self.model_size = model_size or os.getenv("WHISPER_MODEL_SIZE", "base")
        self.device = device or os.getenv("WHISPER_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        self._model = None

    def _load(self):
        """Load the model on first use, not at import.

        Loading at import would mean the worker cannot start until the weights
        are on disk, and would download them again on every cold start. Lazy
        loading keeps startup fast and failures legible.
        """
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - depends on install
                raise TranscriptionError(
                    "faster-whisper is not installed; add it to requirements.txt "
                    "or configure a different transcription provider"
                ) from exc

            log.info(
                "loading whisper model %s (%s, %s)",
                self.model_size, self.device, self.compute_type,
            )
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def transcribe(self, audio_path: Path) -> tuple[list[TranscriptSegment], str]:
        model = self._load()
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
        except Exception as exc:  # noqa: BLE001 - the library raises broadly
            raise TranscriptionError(f"transcription failed: {exc}") from exc

        segments: list[TranscriptSegment] = []
        for index, segment in enumerate(raw_segments):
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

        if not segments:
            raise TranscriptionError(
                "no speech was found in this video; it may be silent or music only"
            )

        language = getattr(info, "language", None) or "unknown"
        log.info(
            "transcribed %d segments (%s) with %s",
            len(segments), language, self.name,
        )
        return segments, language


class NullTranscriptionProvider(TranscriptionProvider):
    """Used when transcription is switched off.

    Kept as a real provider rather than a None check so the pipeline has one
    code path. A job simply arrives at the cut step with no transcript.
    """

    name = "disabled"

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
        name = os.getenv("TRANSCRIPTION_PROVIDER", "faster-whisper")
        provider_class = _PROVIDERS.get(name)
        if provider_class is None:
            raise TranscriptionError(
                f"unknown transcription provider {name!r}; "
                f"choose one of {', '.join(sorted(_PROVIDERS))}"
            )
        _instance = provider_class()
    return _instance


def set_provider(provider: TranscriptionProvider | None) -> None:
    """Override the provider. Used by tests to avoid loading real weights."""
    global _instance
    _instance = provider
