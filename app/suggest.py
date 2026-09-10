"""Clip suggestions from a language model.

The one rule that makes this safe: **the model never returns a timestamp.**

Asked for `start_seconds`, a model will confidently return 847 when nothing
happens at 847. That number arrives inside valid JSON, passes every schema
check, and produces a clip cut in the wrong place. Structured output guarantees
the shape of an answer, never its truth.

So the model returns transcript *segment numbers*. We look up the real start
and end from Whisper's measured timings. The model chooses which sentences are
interesting; the clock is never its to invent. A number that does not exist
fails a lookup and is dropped, which is a visible non-result rather than a
silently wrong cut.

Providers: Ollama (a local model on this machine, the default and free),
Anthropic (hosted Claude, optional), or disabled. The safety barrier is the
same for all of them.
"""
from __future__ import annotations

import json
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.timestamps import format_timestamp
from app.transcription import TranscriptSegment

log = logging.getLogger(__name__)

OLLAMA_DEFAULT_MODEL = "qwen2.5:7b-instruct"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"

# Rough token estimate for sizing windows. Whisper text is ordinary prose, and
# four characters per token is a safe over-estimate for it.
CHARS_PER_TOKEN = 4

SYSTEM_PROMPT = """\
You find the moments in a long video that work as standalone short clips.

The transcript arrives as numbered segments, one per line:
    [12] (00:04:17) the text of that segment

Choose the clips you would cut. Identify each by the segment numbers it starts
and ends on.

A good clip:
- Stands on its own. A viewer who has not seen the rest can follow it.
- Opens on a hook: a claim, a question, a number, a story opening, or a
  contrarian statement. Not "so", "and yeah", or the tail of an earlier answer.
- Resolves. It does not stop mid-thought.
- Is worth attention: a specific insight, a strong opinion, a concrete story, a
  surprising fact. Not pleasantries or admin.

Rules:
- Use ONLY segment numbers that appear in the transcript you were given.
- start_segment must be less than or equal to end_segment.
- Clips must not overlap.
- Prefer 20 to 90 seconds of speech per clip.
- List them in the order they occur.
- If nothing is clip-worthy, return an empty list. Do not pad.
- Respond with JSON only, matching the schema. No commentary.
"""


class SuggestedClip(BaseModel):
    start_segment: int = Field(description="Segment number the clip starts on")
    end_segment: int = Field(description="Segment number the clip ends on")
    title: str = Field(description="Short label for the clip, 3 to 7 words")
    reason: str = Field(description="One sentence on why this works as a clip")


class SuggestionResponse(BaseModel):
    clips: list[SuggestedClip]


@dataclass(frozen=True)
class ResolvedSuggestion:
    """A suggestion after its segment numbers became real timestamps."""

    start: float
    end: float
    title: str
    reason: str
    start_segment: int
    end_segment: int

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


class SuggestionError(RuntimeError):
    """Suggestions could not be produced."""


# -------------------------------------------------------------- providers ----

class SuggestionProvider(ABC):
    name: str = "abstract"
    # False means the pipeline records "switched off" and moves on without
    # asking anything. The pipeline checks this, not the settings, so a test
    # or an operator can swap in a provider without touching configuration.
    enabled: bool = True
    # Transcripts longer than this, in estimated tokens, are sent in windows.
    max_input_tokens: int = 1_000_000

    @abstractmethod
    def propose(self, transcript: str, target_count: int, segment_count: int) -> SuggestionResponse:
        """Ask the model. Returns the raw, unresolved answer."""


def _user_message(transcript: str, target_count: int, segment_count: int) -> str:
    return (
        f"Find up to {target_count} clips in this transcript. It has "
        f"{segment_count} segments, numbered 0 to {segment_count - 1}.\n\n{transcript}"
    )


class OllamaProvider(SuggestionProvider):
    """A local model served by Ollama on this machine.

    Free per video, nothing leaves the server. A 7B model is less sharp than a
    frontier model at judging what stands alone, but the segment-number design
    means its worst case is a dull pick, never a wrong cut.
    """

    name = "ollama"

    def __init__(self):
        self.url = settings.ollama_url
        self.model = settings.suggest_model or OLLAMA_DEFAULT_MODEL
        self.num_ctx = settings.ollama_num_ctx
        self.timeout = settings.suggest_timeout_seconds
        # Leave room for the system prompt, the framing and the answer.
        self.max_input_tokens = max(1024, int(self.num_ctx * 0.7))

    def propose(self, transcript: str, target_count: int, segment_count: int) -> SuggestionResponse:
        payload = {
            "model": self.model,
            "stream": False,
            # A JSON schema here constrains generation to the shape we parse.
            "format": SuggestionResponse.model_json_schema(),
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(transcript, target_count, segment_count)},
            ],
        }
        try:
            response = httpx.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SuggestionError(
                f"could not reach Ollama at {self.url}: {exc}. Is the ollama "
                f"service running and has `ollama pull {self.model}` completed?"
            ) from exc
        if response.status_code >= 400:
            raise SuggestionError(
                f"Ollama returned {response.status_code}: {response.text[:300]}"
            )

        try:
            content = response.json()["message"]["content"]
        except (ValueError, KeyError) as exc:
            raise SuggestionError("Ollama returned an unexpected response shape") from exc

        try:
            return SuggestionResponse.model_validate_json(content)
        except ValidationError as exc:
            raise SuggestionError(f"the model's answer did not match the schema: {exc}") from exc


class AnthropicProvider(SuggestionProvider):
    """Hosted Claude. Optional; needs ANTHROPIC_API_KEY and costs per video."""

    name = "anthropic"

    def __init__(self, client=None):
        self.model = settings.suggest_model or ANTHROPIC_DEFAULT_MODEL
        self._client = client
        self.enabled = bool(client) or bool(settings.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install
                raise SuggestionError("the anthropic package is not installed") from exc
            if not settings.anthropic_api_key:
                raise SuggestionError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def propose(self, transcript: str, target_count: int, segment_count: int) -> SuggestionResponse:
        client = self._get_client()
        try:
            response = client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                thinking={"type": "adaptive"},
                messages=[{"role": "user",
                           "content": _user_message(transcript, target_count, segment_count)}],
                output_format=SuggestionResponse,
            )
        except Exception as exc:  # noqa: BLE001 - network, auth, rate limits
            raise SuggestionError(f"could not get suggestions: {exc}") from exc
        if response.stop_reason == "refusal":
            raise SuggestionError("the model declined to analyse this transcript")
        if response.parsed_output is None:
            raise SuggestionError("the model returned no usable suggestions")
        return response.parsed_output


class DisabledProvider(SuggestionProvider):
    name = "disabled"
    enabled = False

    def propose(self, transcript: str, target_count: int, segment_count: int) -> SuggestionResponse:
        raise SuggestionError("clip suggestions are disabled")


_PROVIDERS: dict[str, type[SuggestionProvider]] = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "disabled": DisabledProvider,
}

_instance: SuggestionProvider | None = None


def get_provider() -> SuggestionProvider:
    global _instance
    if _instance is None:
        provider_class = _PROVIDERS.get(settings.suggest_provider)
        if provider_class is None:
            raise SuggestionError(
                f"unknown suggestion provider {settings.suggest_provider!r}; "
                f"choose one of {', '.join(sorted(_PROVIDERS))}"
            )
        _instance = provider_class()
    return _instance


def set_provider(provider: SuggestionProvider | None) -> None:
    """Override the provider. Used by tests so nothing calls a real model."""
    global _instance
    _instance = provider


# --------------------------------------------------------------- pipeline ----

def format_transcript(segments: Sequence[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{s.index}] ({format_timestamp(s.start)[:8]}) {s.text}" for s in segments
    )


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def window_segments(
    segments: Sequence[TranscriptSegment], max_tokens: int
) -> list[list[TranscriptSegment]]:
    """Split a transcript into windows the model can hold at once.

    Windows break on segment boundaries, never mid-sentence, so every number
    the model can cite still refers to a whole segment.
    """
    windows: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    current_tokens = 0
    for segment in segments:
        cost = estimate_tokens(f"[{segment.index}] (00:00:00) {segment.text}\n")
        if current and current_tokens + cost > max_tokens:
            windows.append(current)
            current, current_tokens = [], 0
        current.append(segment)
        current_tokens += cost
    if current:
        windows.append(current)
    return windows


def resolve(
    raw: Sequence[SuggestedClip], segments: Sequence[TranscriptSegment]
) -> list[ResolvedSuggestion]:
    """Turn segment numbers into real timestamps, discarding anything invalid.

    This is the safety barrier. Every rejection here is a case that would have
    become a wrong cut if the model had been asked for seconds directly.
    """
    by_index = {segment.index: segment for segment in segments}
    resolved: list[ResolvedSuggestion] = []

    for clip in raw:
        start_segment = by_index.get(clip.start_segment)
        end_segment = by_index.get(clip.end_segment)

        if start_segment is None or end_segment is None:
            log.warning(
                "dropped suggestion %r: segments %s-%s are not in the transcript",
                clip.title, clip.start_segment, clip.end_segment,
            )
            continue

        if start_segment.start >= end_segment.end:
            log.warning("dropped suggestion %r: it ends before it starts", clip.title)
            continue

        resolved.append(
            ResolvedSuggestion(
                start=start_segment.start,
                end=end_segment.end,
                title=clip.title.strip() or "Untitled clip",
                reason=clip.reason.strip(),
                start_segment=clip.start_segment,
                end_segment=clip.end_segment,
            )
        )

    resolved.sort(key=lambda s: s.start)

    # Overlaps are legal for hand-written cut lists but never intended here,
    # and the prompt forbids them. Keep the earlier of any overlapping pair.
    deduped: list[ResolvedSuggestion] = []
    for suggestion in resolved:
        if deduped and suggestion.start < deduped[-1].end:
            log.warning("dropped suggestion %r: overlaps the previous one", suggestion.title)
            continue
        deduped.append(suggestion)

    return deduped


def suggest_clips(
    segments: Sequence[TranscriptSegment],
    *,
    target_count: int | None = None,
    provider: SuggestionProvider | None = None,
) -> list[ResolvedSuggestion]:
    """Ask the model which moments are worth cutting.

    Returns resolved suggestions with timestamps taken from the transcript.
    Raises :class:`SuggestionError` if the model cannot be reached; callers
    treat that as "no suggestions", never as a failed job, because the manual
    tools always remain.
    """
    if not segments:
        return []

    provider = provider or get_provider()
    target_count = target_count or settings.suggestion_count
    windows = window_segments(segments, provider.max_input_tokens)

    raw: list[SuggestedClip] = []
    for number, window in enumerate(windows, start=1):
        # Spread the target across windows in proportion to their length, so a
        # long video does not get all its picks from the first ten minutes.
        share = max(1, math.ceil(target_count * len(window) / len(segments)))
        transcript = format_transcript(window)
        log.info(
            "asking %s for %d clips in window %d/%d (%d segments, ~%d tokens)",
            provider.name, share, number, len(windows), len(window), estimate_tokens(transcript),
        )
        answer = provider.propose(transcript, share, len(segments))
        raw.extend(answer.clips)

    resolved = resolve(raw, segments)
    log.info(
        "suggested %d clips from %d segments (%d proposed, %d dropped as invalid)",
        len(resolved), len(segments), len(raw), len(raw) - len(resolved),
    )
    return resolved


def suggestions_to_json(suggestions: Sequence[ResolvedSuggestion]) -> list[dict]:
    return [
        {
            "start": s.start, "end": s.end, "title": s.title, "reason": s.reason,
            "start_segment": s.start_segment, "end_segment": s.end_segment,
        }
        for s in suggestions
    ]
