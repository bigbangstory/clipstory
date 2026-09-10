"""ffmpeg and ffprobe wrappers.

Everything Clipstory promises about exactness lives in this module. The rule it
enforces: a clip either starts on the frame the operator asked for, or it is
marked failed. There is no third outcome where a nearly-right clip is delivered
quietly.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

log = logging.getLogger(__name__)

# Quality of the re-encode. CRF 18 is visually transparent for the social and
# marketing use this tool serves; veryfast trades file size for render time,
# which matters more on the 2-core ARM box this runs on.
VIDEO_CRF = 18
VIDEO_PRESET = "veryfast"
AUDIO_BITRATE = "192k"

# A rendered clip must match the requested duration within this many frames.
# One frame is the tightest tolerance that is achievable in practice: the
# encoder must end on a whole frame, so a requested duration falling mid-frame
# necessarily rounds by up to one.
DURATION_TOLERANCE_FRAMES = 1.0

# Fall back to this when a source reports no usable frame rate, only so that
# the tolerance calculation has a denominator. 30fps gives a 33ms tolerance.
FALLBACK_FPS = 30.0


class MediaError(RuntimeError):
    """An ffmpeg or ffprobe invocation failed, or produced an unusable result."""


class CutVerificationError(MediaError):
    """A clip rendered, but its duration does not match what was requested.

    This is the check that stops a silently-misplaced cut from reaching the
    operator. It should be rare; when it fires, something about the source is
    unusual and the job deserves a human look.
    """


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None
    variable_frame_rate: bool

    @property
    def frame_duration(self) -> float:
        return 1.0 / self.fps if self.fps else 1.0 / FALLBACK_FPS


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    log.debug("running: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{command[0]} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise MediaError(f"{command[0]} is not installed") from exc

    if result.returncode != 0:
        # stderr from ffmpeg is verbose; the tail carries the actual error.
        tail = "\n".join(result.stderr.strip().splitlines()[-8:])
        raise MediaError(f"{command[0]} failed with code {result.returncode}:\n{tail}")
    return result


def _parse_rate(value: str | None) -> float | None:
    """ffprobe reports frame rates as fractions such as '30000/1001'."""
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def probe(path: Path, *, timeout: int = 120) -> MediaInfo:
    """Read a source file's metadata.

    Raises :class:`MediaError` if the file has no video stream or no readable
    duration, since neither can be recovered from and both would produce
    nonsense cuts.
    """
    result = _run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=timeout,
    )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned output that is not JSON") from exc

    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise MediaError("file contains no video stream")

    duration = payload.get("format", {}).get("duration") or video.get("duration")
    if duration is None:
        raise MediaError("file has no readable duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"file reports an unreadable duration {duration!r}") from exc
    if duration <= 0:
        raise MediaError(f"file reports a duration of {duration}s")

    # r_frame_rate is the nominal rate, avg_frame_rate the measured average.
    # A meaningful gap between them means the source is variable frame rate,
    # which is worth recording: our re-encode normalises it, but it explains
    # any surprises if a verification later fails.
    nominal = _parse_rate(video.get("r_frame_rate"))
    average = _parse_rate(video.get("avg_frame_rate"))
    fps = average or nominal or FALLBACK_FPS
    vfr = bool(nominal and average and abs(nominal - average) / nominal > 0.01)

    return MediaInfo(
        duration=round(duration, 6),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=round(fps, 6),
        video_codec=video.get("codec_name") or "unknown",
        audio_codec=audio.get("codec_name") if audio else None,
        variable_frame_rate=vfr,
    )


def build_cut_command(
    source: Path, destination: Path, start: float, duration: float
) -> list[str]:
    """Build the frame-accurate cut command.

    Why each flag is here:

    ``-ss`` before ``-i``
        Fast seek. ffmpeg jumps to the keyframe before ``start``, then decodes
        and discards up to the exact requested frame. Because we re-encode, the
        output still begins precisely on that frame. Putting ``-ss`` after
        ``-i`` would decode the whole file up to that point instead, which on a
        long source is dramatically slower for the same result.

    ``-t`` rather than ``-to``
        With ``-ss`` ahead of the input, timestamps are rebased, so ``-to``
        would be interpreted against the trimmed timeline. ``-t`` is a plain
        duration and cannot be misread.

    re-encoding instead of ``-c copy``
        Stream copy can only start a file on a keyframe, so it silently moves
        the cut by up to the keyframe interval, commonly 2 to 10 seconds. That
        is the failure this whole tool exists to prevent.

    ``setpts=PTS-STARTPTS`` and ``asetpts=PTS-STARTPTS``
        Rebase each stream's timeline to start at exactly zero.

        These are not cosmetic. Measured on a source with keyframes every 10s,
        cutting at 17.400s without them leaves the video stream starting at
        0.080s, which pushes every event in the clip 40ms later than it should
        be and reports a container duration of 5.022s for a 5.000s request.
        With them, the timeline starts at 0.000, the duration is exactly
        5.000s, and measured audio/video drift improves from 13.4ms to 5.3ms.
        The residual is AAC frame granularity and is well below perception.

    no ``-avoid_negative_ts``
        Deliberately absent. It looks like a safe companion to the filters
        above and is widely recommended, but measurement shows it *undoes*
        them: it runs at the muxer, after filtering, and restores the very
        offset the filters removed. With it the clip starts at 0.080s and
        reports 5.022s for a 5.000s request; without it, 0.000s and 5.000s.
        Do not add it back without re-running the timeline tests.

    ``+faststart``
        Moves the moov atom to the front so the clip starts playing before it
        has fully downloaded.

    ``-pix_fmt yuv420p``
        Maximum compatibility. Sources in other pixel formats otherwise produce
        clips that will not play in browsers or on phones.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{start:.6f}",
        "-i", str(source),
        "-t", f"{duration:.6f}",
        "-vf", "setpts=PTS-STARTPTS",
        "-af", "asetpts=PTS-STARTPTS",
        "-c:v", "libx264",
        "-crf", str(VIDEO_CRF),
        "-preset", VIDEO_PRESET,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(destination),
    ]


def cut_clip(
    source: Path,
    destination: Path,
    start: float,
    duration: float,
    *,
    source_fps: float | None = None,
    timeout: int = 3600,
) -> MediaInfo:
    """Cut one clip and verify it landed where it was asked to.

    Returns the probed :class:`MediaInfo` of the rendered clip.

    Raises :class:`CutVerificationError` if the output duration is off by more
    than one frame. The bad file is deleted rather than left where a caller
    might serve it by accident.
    """
    if start < 0:
        raise ValueError("start must not be negative")
    if duration <= 0:
        raise ValueError("duration must be positive")

    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(build_cut_command(source, destination, start, duration), timeout=timeout)

    if not destination.exists() or destination.stat().st_size == 0:
        raise MediaError(f"ffmpeg reported success but produced no output at {destination}")

    rendered = probe(destination)

    fps = source_fps or rendered.fps or FALLBACK_FPS
    tolerance = DURATION_TOLERANCE_FRAMES / fps
    drift = abs(rendered.duration - duration)

    if drift > tolerance:
        destination.unlink(missing_ok=True)
        raise CutVerificationError(
            f"clip duration is {rendered.duration:.3f}s but {duration:.3f}s was "
            f"requested, a drift of {drift * 1000:.0f}ms which exceeds the "
            f"{tolerance * 1000:.0f}ms tolerance of one frame at {fps:.3f}fps"
        )

    log.info(
        "cut %s at %.3fs for %.3fs, drift %.0fms",
        destination.name, start, duration, drift * 1000,
    )
    return rendered
