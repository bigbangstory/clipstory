"""The maths behind text-based editing.

The operator strikes words out of the transcript; this module turns that into
time ranges to remove, and then into the ranges to keep. It is deliberately
pure: no database, no ffmpeg, no I/O. Every rule here is testable on its own,
which matters because a mistake produces a video that is subtly wrong rather
than obviously broken.

Two ideas carry the whole module:

**Deletions are the stored truth; keep-ranges are derived.** What the operator
sees is words struck through, which is a set of deletions. Keep-ranges are the
complement, computed fresh from the source duration whenever they are needed.
Storing the complement instead would mean rewriting the whole document on every
edit, and would go stale the moment the transcript is regenerated.

**Cuts belong in silence.** A cut placed mid-syllable clicks. Because Whisper
gives us the end of one word and the start of the next, the gap between them is
known, and every deletion is expanded outward to the middle of the surrounding
gaps before it is rendered. The cut then lands where nobody is speaking.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

# Float comparisons on timestamps. A microsecond is far below one audio sample
# at any sane rate, so anything closer than this is the same instant.
EPSILON = 1e-6

# Reasons a range was removed. Carried through so the UI can report "47 filler
# words, 12 silences" and undo one category without touching the operator's own
# edits.
MANUAL = "manual"
FILLER = "filler"
SILENCE = "silence"

# Disfluencies with no meaning to lose. Removing these is what people expect
# from a cleanup button.
DEFAULT_FILLERS = frozenset({"um", "uh", "erm", "uhm", "mm", "hmm", "ah", "er", "eh"})

# Available, but off by default, because each of these is frequently doing real
# work in a sentence. "So" opens an answer; "like" can be a comparison; "right"
# can be agreement. Removing them silently changes what someone said, which is
# not a decision a cleanup button should make on the operator's behalf.
OPTIONAL_FILLERS = frozenset({
    "like", "so", "actually", "basically", "literally", "right", "okay",
    "you know", "i mean", "sort of", "kind of",
})

# Gaps longer than this are shortened to SILENCE_TARGET_SECONDS. Pauses are
# part of speech, so they are trimmed rather than removed: cutting them out
# entirely makes a delivery sound breathless and unnatural.
SILENCE_THRESHOLD_SECONDS = 0.8
SILENCE_TARGET_SECONDS = 0.4

# A render has to leave something behind.
MIN_KEPT_SECONDS = 1.0

# Keeping a sliver of video costs a whole extra segment in the filtergraph for
# a fragment nobody can perceive, so slivers are absorbed into the deletion
# either side of them.
MIN_KEEP_SECONDS = 0.05

_PUNCTUATION = re.compile(r"[^\w']+", re.UNICODE)


@dataclass(frozen=True)
class Word:
    """One spoken word with the timings Whisper measured for it."""

    start: float
    end: float
    text: str

    @property
    def normalised(self) -> str:
        """Lowercased, stripped of punctuation, for matching filler lists.

        Matching is on the whole normalised word, never a substring, so "um"
        cannot fire on "number" and "so" cannot fire on "software".
        """
        return _PUNCTUATION.sub("", self.text.lower()).strip("'")


@dataclass(frozen=True)
class Deletion:
    """A time range to remove, and why."""

    start: float
    end: float
    reason: str = MANUAL

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 6)

    def to_json(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "reason": self.reason}

    @classmethod
    def from_json(cls, payload: dict) -> "Deletion":
        return cls(
            start=float(payload["start"]),
            end=float(payload["end"]),
            reason=str(payload.get("reason") or MANUAL),
        )


class EditError(ValueError):
    """The requested edit cannot be rendered."""


# ------------------------------------------------------------------ words ----

def words_from_segments(segments: Iterable[dict]) -> list[Word]:
    """Flatten stored transcript segments into one ordered word list.

    Accepts the shape written by ``jobs._store_transcript``: each segment row
    carries ``words`` as ``[{"s": start, "e": end, "t": text}, ...]``.
    """
    words: list[Word] = []
    for segment in segments:
        for raw in segment.get("words") or []:
            text = (raw.get("t") or "").strip()
            if not text:
                continue
            start, end = float(raw["s"]), float(raw["e"])
            if end <= start:
                # Whisper occasionally emits a zero-length word. It cannot
                # anchor a cut, so it is skipped rather than trusted.
                continue
            words.append(Word(start=start, end=end, text=text))
    words.sort(key=lambda w: (w.start, w.end))
    return words


# -------------------------------------------------------------- deletions ----

def merge_deletions(deletions: Iterable[Deletion]) -> list[Deletion]:
    """Sort, clamp and coalesce deletions into a disjoint, ordered list.

    Overlapping or touching ranges become one. When two merged ranges disagree
    about why they exist, the result is reported as manual: an operator's own
    edit is the more specific claim, and it must not be undone by "undo all
    filler removal".
    """
    valid = [d for d in deletions if d.end - d.start > EPSILON]
    if not valid:
        return []

    merged: list[Deletion] = []
    for deletion in sorted(valid, key=lambda d: (d.start, d.end)):
        if merged and deletion.start <= merged[-1].end + EPSILON:
            previous = merged[-1]
            reason = previous.reason if previous.reason == deletion.reason else MANUAL
            merged[-1] = Deletion(
                start=previous.start,
                end=max(previous.end, deletion.end),
                reason=reason,
            )
        else:
            merged.append(deletion)
    return merged


def clamp_deletions(deletions: Iterable[Deletion], duration: float) -> list[Deletion]:
    """Drop or trim anything that falls outside the source video."""
    clamped = []
    for deletion in deletions:
        start = max(0.0, deletion.start)
        end = min(duration, deletion.end)
        if end - start > EPSILON:
            clamped.append(replace(deletion, start=start, end=end))
    return clamped


def snap_to_silence(deletions: Iterable[Deletion], words: Sequence[Word]) -> list[Deletion]:
    """Widen each deletion to the middle of the silence around it.

    A deletion arrives as exactly the span of the struck-out words, so its
    edges sit on the first sound in and the last sound out. Cutting there
    clips the attack of a word and pops. Moving each edge to the midpoint of
    the adjacent gap puts the cut where nobody is speaking, and takes the dead
    air around the removed words with it, which is what the operator wanted
    anyway.

    The expansion is bounded by the gap itself, so this can never swallow a
    neighbouring word. With no gap at all, the edge does not move.
    """
    words = sorted(words, key=lambda w: w.start)
    if not words:
        return list(deletions)

    # Sorted once and searched with bisect: a one-hour transcript is tens of
    # thousands of words, and a cleanup pass produces hundreds of deletions,
    # so a linear scan per deletion would be millions of comparisons.
    ends = sorted(w.end for w in words)
    starts = [w.start for w in words]  # already sorted, words are sorted by start

    snapped = []
    for deletion in deletions:
        # Latest word that finishes at or before this deletion begins.
        cut = bisect_right(ends, deletion.start + EPSILON)
        previous_end = ends[cut - 1] if cut else None
        # Earliest word that begins at or after this deletion ends.
        cut = bisect_left(starts, deletion.end - EPSILON)
        next_start = starts[cut] if cut < len(starts) else None

        start = deletion.start
        end = deletion.end
        if previous_end is not None and previous_end < start:
            start = (previous_end + start) / 2
        if next_start is not None and next_start > end:
            end = (end + next_start) / 2
        snapped.append(replace(deletion, start=start, end=end))
    return snapped


def prepare(
    deletions: Iterable[Deletion],
    words: Sequence[Word],
    duration: float,
    *,
    snap: bool = True,
) -> list[Deletion]:
    """Everything a raw deletion list needs before it can be rendered."""
    prepared = clamp_deletions(deletions, duration)
    if snap:
        prepared = snap_to_silence(prepared, words)
        prepared = clamp_deletions(prepared, duration)
    return merge_deletions(prepared)


# ------------------------------------------------------------ keep ranges ----

def keep_ranges(duration: float, deletions: Sequence[Deletion]) -> list[tuple[float, float]]:
    """The complement of the deletions: what actually gets rendered.

    Expects ``deletions`` already merged and clamped (see :func:`prepare`).
    """
    if duration <= 0:
        raise EditError("the source video has no duration")

    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for deletion in deletions:
        if deletion.start - cursor > MIN_KEEP_SECONDS:
            ranges.append((cursor, deletion.start))
        cursor = max(cursor, deletion.end)
    if duration - cursor > MIN_KEEP_SECONDS:
        ranges.append((cursor, duration))

    kept = sum(end - start for start, end in ranges)
    if kept < MIN_KEPT_SECONDS:
        raise EditError(
            f"this edit would leave only {kept:.2f}s of video; "
            "restore something before exporting"
        )
    return ranges


def kept_duration(duration: float, deletions: Sequence[Deletion]) -> float:
    return round(sum(end - start for start, end in keep_ranges(duration, deletions)), 6)


def summarise(deletions: Sequence[Deletion]) -> dict:
    """Counts and seconds per reason, for the UI to report what it removed."""
    summary = {"total": {"count": len(deletions), "seconds": 0.0}}
    for deletion in deletions:
        summary["total"]["seconds"] += deletion.duration
        bucket = summary.setdefault(deletion.reason, {"count": 0, "seconds": 0.0})
        bucket["count"] += 1
        bucket["seconds"] += deletion.duration
    for bucket in summary.values():
        bucket["seconds"] = round(bucket["seconds"], 2)
    return summary


def mark_words(words: Sequence[Word], deletions: Sequence[Deletion]) -> list[bool]:
    """Which words fall inside a deletion, for rendering strike-through.

    A word counts as removed when most of it is inside a deleted range, so a
    cut that clips a few milliseconds off a neighbour does not strike it out.
    """
    flags = []
    for word in words:
        covered = 0.0
        for deletion in deletions:
            overlap = min(word.end, deletion.end) - max(word.start, deletion.start)
            if overlap > 0:
                covered += overlap
        flags.append(covered > (word.end - word.start) / 2)
    return flags


# ------------------------------------------------------------ auto cleanup ----

def _phrase_at(words: Sequence[Word], index: int, length: int) -> str:
    return " ".join(w.normalised for w in words[index:index + length])


def find_fillers(
    words: Sequence[Word], vocabulary: Iterable[str] | None = None
) -> list[Deletion]:
    """Deletions for every filler word or phrase in the transcript.

    Multi-word entries ("you know") are matched greedily and before single
    words, so the whole phrase goes rather than half of it.
    """
    terms = set(vocabulary) if vocabulary is not None else set(DEFAULT_FILLERS)
    if not terms:
        return []
    longest = max(len(term.split()) for term in terms)

    deletions: list[Deletion] = []
    index = 0
    while index < len(words):
        for length in range(min(longest, len(words) - index), 0, -1):
            if _phrase_at(words, index, length) in terms:
                deletions.append(
                    Deletion(
                        start=words[index].start,
                        end=words[index + length - 1].end,
                        reason=FILLER,
                    )
                )
                index += length
                break
        else:
            index += 1
    return deletions


def find_long_silences(
    words: Sequence[Word],
    duration: float,
    *,
    threshold: float = SILENCE_THRESHOLD_SECONDS,
    target: float = SILENCE_TARGET_SECONDS,
) -> list[Deletion]:
    """Deletions that shorten every over-long pause to ``target`` seconds.

    The middle of the gap is removed, leaving ``target/2`` of silence either
    side of the cut. Pauses are not removed outright: speech with its pauses
    stripped out sounds rushed and unnatural, and the point of the button is to
    tighten a recording, not to strip it.

    Silence before the first word and after the last is trimmed on the same
    rule, which is what removes a slow start and a trailing dead end.
    """
    if target >= threshold:
        raise EditError("the silence target must be shorter than the threshold")

    gaps: list[tuple[float, float]] = []
    if words:
        if words[0].start > threshold:
            gaps.append((0.0, words[0].start))
        for previous, following in zip(words, words[1:]):
            if following.start - previous.end > threshold:
                gaps.append((previous.end, following.start))
        if duration - words[-1].end > threshold:
            gaps.append((words[-1].end, duration))
    elif duration > threshold:
        gaps.append((0.0, duration))

    # Keep `target` seconds of the pause, split evenly either side of the cut,
    # and delete the rest of it. The padding is half the *target*, not half the
    # amount removed; getting that backwards keeps the long pause and deletes
    # the short one.
    pad = target / 2
    deletions = []
    for start, end in gaps:
        if (end - start) - target > EPSILON:
            deletions.append(Deletion(start=start + pad, end=end - pad, reason=SILENCE))
    return deletions


def auto_cleanup(
    words: Sequence[Word],
    duration: float,
    *,
    remove_fillers: bool = True,
    filler_vocabulary: Iterable[str] | None = None,
    shorten_silences: bool = True,
    silence_threshold: float = SILENCE_THRESHOLD_SECONDS,
    silence_target: float = SILENCE_TARGET_SECONDS,
) -> list[Deletion]:
    """Both cleanup passes, as one list of deletions.

    Returned unsnapped and unmerged; :func:`prepare` does that at render time
    so that stored deletions stay aligned with the words the operator sees
    struck out.
    """
    deletions: list[Deletion] = []
    if remove_fillers:
        deletions.extend(find_fillers(words, filler_vocabulary))
    if shorten_silences:
        deletions.extend(
            find_long_silences(
                words, duration, threshold=silence_threshold, target=silence_target
            )
        )
    return deletions


def without_reason(deletions: Iterable[Deletion], reason: str) -> list[Deletion]:
    """Drop one category, keeping the operator's own edits. Powers "undo all
    filler removal" without discarding hand-made cuts."""
    return [d for d in deletions if d.reason != reason]
