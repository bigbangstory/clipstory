"""Output filenames.

Clips are numbered in the order the operator pasted them, not in time order.
The paste order is the deliverable order, so a moment from late in the video
pasted first is still clip 01.
"""
from __future__ import annotations

import re
import unicodedata

MAX_SLUG_LENGTH = 60
MAX_LABEL_LENGTH = 40


def slugify(text: str, *, max_length: int = MAX_SLUG_LENGTH) -> str:
    """Reduce arbitrary text to a safe, readable filename fragment."""
    # Decompose accents to their base letters so "Café" survives as "cafe"
    # rather than collapsing to "caf".
    normalised = unicodedata.normalize("NFKD", text)
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if len(hyphenated) > max_length:
        hyphenated = hyphenated[:max_length].rstrip("-")
    return hyphenated or "clip"


def source_slug(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return slugify(stem)


def sequence_width(total_clips: int) -> int:
    """Zero-padding width. Two digits normally, widening past 99 so that
    filenames still sort correctly in any file browser."""
    return max(2, len(str(max(total_clips, 1))))


def clip_filename(
    source_filename: str, sequence: int, total_clips: int, label: str | None = None
) -> str:
    slug = source_slug(source_filename)
    number = str(sequence).zfill(sequence_width(total_clips))
    parts = [slug, "clip", number]
    if label:
        label_slug = slugify(label, max_length=MAX_LABEL_LENGTH)
        if label_slug and label_slug != "clip":
            parts.append(label_slug)
    return "_".join(parts) + ".mp4"


def edit_filename(source_filename: str) -> str:
    """The full-length edited video, distinct from the numbered clips."""
    return f"{source_slug(source_filename)}_edited.mp4"


def zip_filename(source_filename: str) -> str:
    return f"{source_slug(source_filename)}_clips.zip"
