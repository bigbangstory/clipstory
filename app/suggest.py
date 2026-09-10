"""LLM clip suggestions.

The one rule that makes this safe: **the model never returns a timestamp.**

Asked for `start_seconds`, a model will confidently return 847 when nothing
happens at 847. That number arrives inside valid JSON, passes every schema
check, and produces a clip cut in the wrong place. Structured output guarantees
the shape of an answer, never its truth.

So the model returns transcript *segment indices*. We look up the real start
and end from Whisper's measured word timings. The model chooses which sentences
are interesting; the clock is never its to invent. An index that does not exist
fails a lookup and is dropped, which is a visible non-result rather than a
silently wrong cut.

Suggestions are a proposal. They pre-fill the cut box for the operator to edit,
and still pass through the same parser, the same validation and the same
confirmation table as anything typed by hand.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.transcription import TranscriptSegment

log = logging.getLogger(__name__)

MODEL = os.getenv("SUGGEST_MODEL", "claude-opus-5")
MAX_TOKENS = 16000

# Long transcripts are packed whole rather than sampled: a three-hour interview
# is plausibly 60k tokens, well inside the 1M context window, and sampling
# would hide exactly the moments worth finding.
SYSTEM_PROMPT = """\
You find the moments in a long video that work as standalone short clips.

You will receive a transcript as numbered segments, one per line, in the form:
    [12] (00:04:17) the text of that segment

Return the clips you would cut, each identified by the segment numbers it
starts and ends on.

What makes a good clip:
- It stands on its own. Someone who has not seen the rest of the video should
  follow it without context.
- It opens on a hook: a claim, a question, a number, a story opening, or a
  contrarian statement. Not "so", "and yeah", or the tail of an earlier answer.
- It resolves. It does not stop mid-thought or mid-sentence.
- It is worth someone's attention: a specific insight, a strong opinion, a
  concrete story, a surprising fact. Not pleasantries, admin, or throat-clearing.

Rules you must follow:
- Use ONLY segment numbers that appear in the transcript given to you.
- start_segment must be less than or equal to end_segment.
- Clips must not overlap each other.
- Prefer clips between 20 and 90 seconds of speech.
- Order them as they occur in the video.
- If the transcript genuinely contains nothing clip-worthy, return an empty
  list. A short honest answer is better than padding.

Never invent a timestamp. You are choosing segment numbers only; the timings
are taken from the transcript itself.
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


def format_transcript(segments: Sequence[TranscriptSegment]) -> str:
    from app.timestamps import format_timestamp

    return "\n".join(
        f"[{s.index}] ({format_timestamp(s.start)[:8]}) {s.text}" for s in segments
    )


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on install
        raise SuggestionError(
            "the anthropic package is not installed; add it to requirements.txt"
        ) from exc

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SuggestionError(
            "ANTHROPIC_API_KEY is not set, so clip suggestions are unavailable. "
            "Everything else works; paste your own timestamps instead."
        )
    return anthropic.Anthropic()


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
            # The model referred to a segment that does not exist. Had it been
            # asked for seconds, this would have been an unnoticed wrong cut.
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
                title=clip.title.strip(),
                reason=clip.reason.strip(),
                start_segment=clip.start_segment,
                end_segment=clip.end_segment,
            )
        )

    resolved.sort(key=lambda s: s.start)

    # Overlaps are legal for hand-written cut lists but never intended here,
    # and the prompt forbids them. Drop the later of any overlapping pair.
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
    target_count: int = 8,
    client: Any = None,
) -> list[ResolvedSuggestion]:
    """Ask the model which moments are worth cutting.

    Returns resolved suggestions with timestamps taken from the transcript.
    Raises :class:`SuggestionError` if the model cannot be reached; callers
    treat that as "no suggestions", never as a failed job, because suggestions
    are a convenience and the operator can always type their own.
    """
    if not segments:
        return []

    client = client or _client()
    transcript = format_transcript(segments)

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # The instructions are identical on every call, so caching
                    # the prefix makes repeat runs on the same video cheap.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            thinking={"type": "adaptive"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Find up to {target_count} clips in this transcript. "
                        f"The video has {len(segments)} segments, numbered 0 to "
                        f"{len(segments) - 1}.\n\n{transcript}"
                    ),
                }
            ],
            output_format=SuggestionResponse,
        )
    except Exception as exc:  # noqa: BLE001 - network, auth, rate limits
        raise SuggestionError(f"could not get suggestions: {exc}") from exc

    if response.stop_reason == "refusal":
        raise SuggestionError("the model declined to analyse this transcript")

    parsed = response.parsed_output
    if parsed is None:
        raise SuggestionError("the model returned no usable suggestions")

    resolved = resolve(parsed.clips, segments)
    log.info(
        "suggested %d clips from %d segments (%d proposed, %d dropped as invalid)",
        len(resolved), len(segments), len(parsed.clips), len(parsed.clips) - len(resolved),
    )
    return resolved


def to_cut_list(suggestions: Sequence[ResolvedSuggestion]) -> str:
    """Render suggestions as text for the cut box, so they can be edited."""
    from app.timestamps import format_timestamp

    if not suggestions:
        return ""
    lines = ["# Suggested clips. Edit freely, then check the list."]
    for suggestion in suggestions:
        start = format_timestamp(suggestion.start)
        end = format_timestamp(suggestion.end)
        lines.append(f"{start} - {end} | {suggestion.title}")
    return "\n".join(lines) + "\n"
