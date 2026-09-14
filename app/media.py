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
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Sequence

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
    # The exact rate as a fraction, not just the rounded float. NTSC rates are
    # 30000/1001 and friends, and audio/video locking needs the true ratio:
    # approximating 29.97 leaves a systematic bias that still drifts apart over
    # hundreds of cuts.
    frame_rate: Fraction = Fraction(FALLBACK_FPS).limit_denominator(1000)

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

    exact = None
    for candidate in (video.get("avg_frame_rate"), video.get("r_frame_rate")):
        if candidate and candidate not in ("0/0", "N/A"):
            try:
                exact = Fraction(candidate)
            except (ValueError, ZeroDivisionError):
                exact = None
            if exact and exact > 0:
                break
            exact = None

    return MediaInfo(
        frame_rate=exact or Fraction(fps).limit_denominator(120000),
        duration=round(duration, 6),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=round(fps, 6),
        video_codec=video.get("codec_name") or "unknown",
        audio_codec=audio.get("codec_name") if audio else None,
        variable_frame_rate=vfr,
    )


def probe_streams(path: Path, *, timeout: int = 120) -> dict[str, float | int | None]:
    """Per-stream durations and the video frame count.

    Separate from :func:`probe` because the container duration is the maximum
    across streams and picks up AAC priming padding, which at 60fps is already
    more than one frame. Verifying an edit needs the streams themselves.
    """
    result = _run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-count_frames", str(path)],
        timeout=timeout,
    )
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned output that is not JSON") from exc

    def number(value, cast):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "video_duration": number(video and video.get("duration"), float),
        "audio_duration": number(audio and audio.get("duration"), float),
        "video_frames": number(
            video and (video.get("nb_read_frames") or video.get("nb_frames")), int
        ),
    }


def extract_audio(source: Path, destination: Path, *, timeout: int = 3600) -> Path:
    """Pull the audio out as 16 kHz mono 16-bit PCM.

    That format is what Whisper models expect, and converting once here means
    the transcriber never has to think about the source's codec or channel
    layout. It is also small: 32 KB/s, so about 115 MB per hour of video.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(source),
            "-vn",                      # drop video, we only want the audio
            "-acodec", "pcm_s16le",
            "-ar", "16000",             # Whisper's native sample rate
            "-ac", "1",                 # mono
            str(destination),
        ],
        timeout=timeout,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise MediaError("audio extraction produced an empty file")
    return destination


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


# A deletion list longer than this is refused as nonsense rather than attempted.
# Note this is a sanity check on the edit, not a resource limit: with the
# select-based renderer below, memory and time are flat in the number of cuts.
MAX_EDIT_SEGMENTS = 2000

# Ramp either side of each join, as insurance against a click. Deletions are
# snapped into silence upstream, so this is belt and braces.
JOIN_FADE_SECONDS = 0.005

# Sample rates tried when looking for one that fits a whole number of samples
# into one video frame. See audio_lock().
_LOCK_RATES = (48000, 96000, 60000, 30000, 24000, 88200, 44100)
OUTPUT_SAMPLE_RATE = 48000


def audio_lock(fps: Fraction) -> tuple[int, int] | None:
    """Find a sample rate where one video frame is a whole number of samples.

    This is what keeps audio and video together across hundreds of cuts.
    ``aselect`` can only drop whole audio frames (about 21ms at 48kHz), so each
    cut keeps a slightly different amount of audio than video, and because
    ``asetpts`` renumbers everything contiguously those errors accumulate as a
    random walk rather than cancelling.

    Measured on a 300s source with 281 cuts: plain ``aselect`` ended 54ms out of
    sync. Repacketising the audio into chunks exactly one frame long first, so a
    segment covering N frames keeps exactly N chunks by construction, ended 0ms
    out.

    Returns ``(sample_rate, samples_per_frame)``, or None when no rate divides
    evenly, in which case the caller falls back to unlocked audio. The exactness
    matters: for 29.97fps, 48000 gives 1601.6 samples per frame, and rounding
    that to 1602 leaves a systematic bias that still drifts. 60000 gives exactly
    2002.
    """
    for rate in _LOCK_RATES:
        if (rate * fps.denominator) % fps.numerator == 0:
            return rate, rate * fps.denominator // fps.numerator
    return None


def align_ranges_to_frames(
    ranges: Sequence[tuple[float, float]], fps: float
) -> list[tuple[int, int]]:
    """Turn time ranges into half-open frame index ranges, merging neighbours.

    Frame indices, not seconds, are the real domain here. Everything downstream
    (how many frames to expect, where the joins fall, what to put in the select
    expression) derives from them, and deriving it all from one rounding means
    two parts can never disagree. Rounding seconds a second time is exactly how
    an off-by-one frame creeps in: Python rounds halves to even, so 324.5 goes
    down and 49.5 goes up.

    Merging matters because a deletion shorter than one frame cannot exist, so
    both its edges land on the same frame and the ranges either side become
    adjacent. Merging avoids paying for a cut that removes nothing.
    """
    if fps <= 0:
        fps = FALLBACK_FPS

    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        first, last = round(start * fps), round(end * fps)
        if last <= first:
            continue  # collapsed to nothing by rounding
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def frame_ranges_to_seconds(
    frame_ranges: Sequence[tuple[int, int]], fps: float
) -> list[tuple[float, float]]:
    """Frame indices back to the boundaries used in a select expression.

    Half a frame early, deliberately. The selection test is ``t >= start``, and
    a boundary sitting exactly on a frame's own timestamp rounds unpredictably,
    which yields one extra frame per segment about half the time. Placed in the
    gap between two frames the comparison is unambiguous, and the output frame
    count is exactly the sum of the index ranges, verified here from 2 up to
    281 segments with deliberately awkward boundaries.
    """
    if fps <= 0:
        fps = FALLBACK_FPS
    return [((first - 0.5) / fps, (last - 0.5) / fps) for first, last in frame_ranges]


def expected_frame_count(frame_ranges: Sequence[tuple[int, int]]) -> int:
    """Exactly how many frames an aligned edit will produce."""
    return sum(last - first for first, last in frame_ranges)


def _selection_expression(ranges: Sequence[tuple[float, float]]) -> str:
    """The ffmpeg expression that keeps exactly these ranges.

    ``gte(t,S)*lt(t,E)`` rather than ``between(t,S,E)``. ``between`` is
    inclusive at *both* ends, so it keeps one extra frame per segment. Measured:
    200 segments with ``between`` produced 2600 frames where 2400 were wanted,
    an 86.7s output for an 80.0s request, with audio at 80.256s. Six seconds of
    silent audio/video desync, from one function name.
    """
    return "+".join(f"(gte(t,{s:.6f})*lt(t,{e:.6f}))" for s, e in ranges)


def _join_fades(frame_ranges: Sequence[tuple[int, int]], fps: float) -> str:
    """Fade pairs at every join, positioned in *output* time.

    With select there are no per-segment streams to ramp, so the ramps go on the
    finished timeline at the cumulative join positions.
    """
    if len(frame_ranges) < 2:
        return ""
    ramp = JOIN_FADE_SECONDS
    chains, elapsed_frames = [], 0
    for first, last in frame_ranges[:-1]:
        elapsed_frames += last - first
        at = elapsed_frames / fps
        if at <= ramp:
            continue
        chains.append(
            f"afade=t=out:st={at - ramp:.6f}:d={ramp}:curve=tri,"
            f"afade=t=in:st={at:.6f}:d={ramp}:curve=tri"
        )
    return ("," + ",".join(chains)) if chains else ""


def build_edit_filters(
    frame_ranges: Sequence[tuple[int, int]],
    fps: float,
    *,
    has_audio: bool = True,
    lock: tuple[int, int] | None = None,
) -> tuple[str, str | None]:
    """The video and audio filter chains for an edited render.

    ``select`` rather than ``trim``/``concat``. That is not a style preference:
    trim+concat holds the whole span from the first kept range to the last in
    memory at once, so its cost is set by the length of the *source*, not by the
    number of cuts. Measured on this machine, a 60-second 1080p source with 20
    segments peaked at **4.40 GB**; the same edit through select peaked at
    **0.19 GB** and ran 2.6x faster. On an hour of 1080p the trim+concat shape
    would need hundreds of gigabytes and be killed. The earlier measurement that
    made it look acceptable used a 320x180 source, where the per-second cost is
    small enough to hide the problem entirely.

    The trade is that select cannot reorder segments, only drop them. Deleting
    is what the transcript editor does, so nothing is lost today; reordering
    would need the chunked concat path instead.
    """
    if not frame_ranges:
        raise ValueError("an edit must keep at least one range")
    if len(frame_ranges) > MAX_EDIT_SEGMENTS:
        raise ValueError(
            f"this edit needs {len(frame_ranges)} segments, over the "
            f"{MAX_EDIT_SEGMENTS} limit; the deletion list is probably wrong"
        )

    expression = _selection_expression(frame_ranges_to_seconds(frame_ranges, fps))
    # setpts=N/(FPS*TB) renumbers the surviving frames from zero at a constant
    # rate. PTS-STARTPTS would only rebase the first one and leave the holes in.
    video = f"select='{expression}',setpts=N/({fps:.10g}*TB)"
    if not has_audio:
        return video, None

    audio = ""
    if lock:
        rate, per_frame = lock
        audio += f"aresample={rate},asetnsamples=n={per_frame}:p=0,"
    audio += f"aselect='{expression}',asetpts=N/SR/TB"
    audio += _join_fades(frame_ranges, fps)
    if lock and lock[0] != OUTPUT_SAMPLE_RATE:
        audio += f",aresample={OUTPUT_SAMPLE_RATE}"
    return video, audio


def render_edit(
    source: Path,
    destination: Path,
    ranges: Sequence[tuple[float, float]],
    *,
    source_fps: float | None = None,
    has_audio: bool = True,
    timeout: int = 7200,
) -> MediaInfo:
    """Render the source with only ``ranges`` kept, joined into one video.

    One decode and one encode pass, with memory flat in both the length of the
    source and the number of cuts.

    Verification asserts the exact frame count rather than an approximate
    duration. Because boundaries are snapped to frame indices, the expected
    count is known precisely, so the check does not need a tolerance that grows
    with the number of cuts. A tolerance like that would hide exactly the bugs
    worth catching. Audio and video lengths are checked against each other too,
    since that is what catches drift a duration check sails past.
    """
    if not ranges:
        raise ValueError("an edit must keep at least one range")

    info = probe(source)
    fps = source_fps or info.fps or FALLBACK_FPS
    aligned = align_ranges_to_frames(ranges, fps)
    if not aligned:
        raise ValueError("nothing survives frame alignment; the edit is too fine")

    expected_frames = expected_frame_count(aligned)
    expected_duration = expected_frames / fps
    has_audio = has_audio and info.audio_codec is not None
    lock = audio_lock(info.frame_rate) if has_audio else None
    if has_audio and lock is None:
        log.warning(
            "no sample rate locks to %s fps; audio may drift over many cuts",
            info.frame_rate,
        )

    video_filter, audio_filter = build_edit_filters(
        aligned, fps, has_audio=has_audio, lock=lock
    )

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(source), "-vf", video_filter]
    if audio_filter:
        command += ["-af", audio_filter]
    command += [
        "-c:v", "libx264",
        "-crf", str(VIDEO_CRF),
        "-preset", VIDEO_PRESET,
        "-pix_fmt", "yuv420p",
    ]
    if audio_filter:
        command += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", str(destination)]

    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        _run(command, timeout=timeout)
    except MediaError:
        # Keep the filters next to the failure so the graph can be inspected.
        destination.with_suffix(".filters.txt").write_text(
            f"-vf {video_filter}\n\n-af {audio_filter or '(none)'}\n"
        )
        destination.unlink(missing_ok=True)
        raise

    if not destination.exists() or destination.stat().st_size == 0:
        raise MediaError(f"ffmpeg reported success but produced no output at {destination}")

    streams = probe_streams(destination)
    rendered_frames = streams.get("video_frames")
    frame = 1.0 / fps

    def reject(message: str) -> None:
        destination.unlink(missing_ok=True)
        raise CutVerificationError(message)

    if rendered_frames is not None and rendered_frames != expected_frames:
        reject(
            f"edited video has {rendered_frames} frames but {expected_frames} were "
            f"expected from {len(aligned)} segments, a difference of "
            f"{rendered_frames - expected_frames} frames"
        )

    video_duration = streams.get("video_duration")
    if video_duration is not None and abs(video_duration - expected_duration) > frame:
        reject(
            f"edited video is {video_duration:.3f}s but {expected_duration:.3f}s was "
            f"expected, a drift of {abs(video_duration - expected_duration) * 1000:.0f}ms"
        )

    audio_duration = streams.get("audio_duration")
    if video_duration is not None and audio_duration is not None:
        skew = abs(audio_duration - video_duration)
        if skew > 2 * frame:
            reject(
                f"audio and video are {skew * 1000:.0f}ms apart in the edited "
                f"video, over the {2 * frame * 1000:.0f}ms limit; they would "
                "drift out of sync"
            )

    rendered = probe(destination)
    log.info(
        "rendered edit %s: %d segments, %d frames, %.3fs, took %.1fs",
        destination.name, len(aligned), expected_frames, rendered.duration,
        time.monotonic() - started,
    )
    return rendered


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
