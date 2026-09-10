"""Parsing and validation of operator-supplied cut points.

This module is deliberately pure: no I/O, no database, no ffmpeg. The exactness
of Clipstory's cuts starts here, so every rule is explicit and every failure
names the line that caused it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Minimum length of a clip. Below this the output is not useful and often
# shorter than a single group of pictures.
MIN_CLIP_SECONDS = 1.0

# Separators accepted between the start and end of a range. Longest first so
# that "->" is matched before "-".
_SEPARATORS = ("->", "to", "-", ",")

_LABEL_SEPARATOR = "|"
_COMMENT_PREFIX = "#"

# HH:MM:SS(.mmm) / MM:SS(.mmm) / SS(.mmm)
_CLOCK = re.compile(
    r"""^
    (?:(?P<h>\d+):)?        # optional hours
    (?:(?P<m>\d{1,2}):)?    # optional minutes
    (?P<s>\d{1,2})          # seconds
    (?:\.(?P<frac>\d{1,6}))?  # optional fractional seconds
    $""",
    re.VERBOSE,
)

_BARE_SECONDS = re.compile(r"^\d+(?:\.\d{1,6})?$")


class TimestampError(ValueError):
    """Raised when a line cannot be parsed or violates a validation rule.

    Carries the 1-based line number so the UI can point at the offending line
    rather than saying the whole paste is bad.
    """

    def __init__(self, line_number: int, line: str, message: str):
        self.line_number = line_number
        self.line = line
        self.message = message
        super().__init__(f"Line {line_number}: {message}\n    {line!r}")


@dataclass(frozen=True)
class CutRange:
    """One requested clip, in seconds from the start of the source."""

    sequence: int
    start: float
    end: float
    label: str | None = None
    line_number: int = 0

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 6)


def parse_timestamp(text: str) -> float:
    """Turn a single timestamp into seconds.

    Accepts ``HH:MM:SS``, ``MM:SS``, ``HH:MM:SS.mmm``, and bare seconds.

    Note that ``MM:SS`` is distinguished from ``HH:MM`` by position, not by
    magnitude: two colon-separated parts are always minutes and seconds. This
    matches how people write video timestamps and how YouTube reads them.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")

    if _BARE_SECONDS.match(text):
        return float(text)

    match = _CLOCK.match(text)
    if not match:
        raise ValueError(f"unrecognised timestamp {text!r}")

    hours = match.group("h")
    minutes = match.group("m")
    seconds = match.group("s")
    frac = match.group("frac")

    # With only one colon the regex fills the hour group, because the minute
    # group is the one that may be skipped. Shift it down: "04:17" is 4m17s.
    if hours is not None and minutes is None:
        hours, minutes = None, hours

    total = float(seconds)
    if minutes is not None:
        total += int(minutes) * 60
    if hours is not None:
        total += int(hours) * 3600
    if frac is not None:
        total += float(f"0.{frac}")

    if minutes is not None and int(minutes) > 59:
        raise ValueError(f"minutes out of range in {text!r}")
    if float(seconds) > 59 and (minutes is not None or hours is not None):
        raise ValueError(f"seconds out of range in {text!r}")

    return round(total, 6)


def format_timestamp(seconds: float) -> str:
    """Render seconds back as ``HH:MM:SS.mmm``, for tables and manifests."""
    if seconds < 0:
        raise ValueError("cannot format a negative timestamp")
    whole = int(seconds)
    millis = round((seconds - whole) * 1000)
    if millis == 1000:  # rounding carried into the next second
        whole += 1
        millis = 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _split_range(body: str) -> tuple[str, str]:
    """Split ``start <sep> end`` on the first separator that is not part of a
    timestamp.

    A bare ``-`` is ambiguous only in theory here, since we do not accept
    negative timestamps, but ``to`` must be matched as a whole word so that it
    does not fire inside some future named format.
    """
    for sep in _SEPARATORS:
        if sep == "to":
            match = re.search(r"\bto\b", body, re.IGNORECASE)
            if match:
                return body[: match.start()], body[match.end() :]
        else:
            index = body.find(sep)
            if index > 0:  # not at position 0, which would mean an empty start
                return body[:index], body[index + len(sep) :]
    raise ValueError(
        "no start/end separator found, expected one of '-', '->', 'to' or ','"
    )


def parse_cut_list(text: str, source_duration: float | None = None) -> list[CutRange]:
    """Parse a pasted cut list into ordered, validated ranges.

    Raises :class:`TimestampError` on the first line that will not parse or
    that breaks a rule. Nothing is rendered until this returns cleanly, so a
    bad paste costs the operator a correction, never a wrong clip.

    ``source_duration`` is optional so the parser can be used before the video
    has been probed; when supplied, ranges past the end of the video are
    rejected.
    """
    ranges: list[CutRange] = []
    sequence = 0

    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(_COMMENT_PREFIX):
            continue

        body, _, label_part = line.partition(_LABEL_SEPARATOR)
        label = label_part.strip() or None

        try:
            start_text, end_text = _split_range(body)
        except ValueError as exc:
            raise TimestampError(index, raw_line, str(exc)) from exc

        try:
            start = parse_timestamp(start_text)
            end = parse_timestamp(end_text)
        except ValueError as exc:
            raise TimestampError(index, raw_line, str(exc)) from exc

        if start >= end:
            raise TimestampError(
                index,
                raw_line,
                f"start ({format_timestamp(start)}) must be before end "
                f"({format_timestamp(end)})",
            )

        if end - start < MIN_CLIP_SECONDS:
            raise TimestampError(
                index,
                raw_line,
                f"clip is {end - start:.3f}s, shorter than the {MIN_CLIP_SECONDS}s minimum",
            )

        if source_duration is not None and end > source_duration:
            raise TimestampError(
                index,
                raw_line,
                f"end ({format_timestamp(end)}) is past the end of the video "
                f"({format_timestamp(source_duration)})",
            )

        sequence += 1
        ranges.append(
            CutRange(
                sequence=sequence,
                start=start,
                end=end,
                label=label,
                line_number=index,
            )
        )

    if not ranges:
        raise ValueError("no cut ranges found; paste at least one line")

    return ranges


def find_overlaps(ranges: Iterable[CutRange]) -> list[tuple[CutRange, CutRange]]:
    """Return pairs of ranges that overlap in time.

    Overlaps are legal (the same moment may belong in two clips) so this is a
    warning surfaced in the confirmation table, never an error.
    """
    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    overlaps: list[tuple[CutRange, CutRange]] = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if second.start >= first.end:
                break  # sorted by start, so nothing later can overlap either
            overlaps.append((first, second))
    return overlaps
