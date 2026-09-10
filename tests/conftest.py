import shutil
import subprocess

import pytest

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(
    not FFMPEG_AVAILABLE, reason="ffmpeg and ffprobe are required for media tests"
)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]


def _font() -> str | None:
    from pathlib import Path

    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


@pytest.fixture(scope="session")
def marked_source(tmp_path_factory):
    """A 30s 25fps test video with keyframes forced every 10 seconds.

    The wide keyframe spacing is the point: it makes the difference between an
    exact cut and a keyframe-snapped one large enough to measure unambiguously.
    Real uploaded footage typically sits somewhere between 2 and 10 seconds.
    """
    if not FFMPEG_AVAILABLE:
        pytest.skip("ffmpeg not available")

    path = tmp_path_factory.mktemp("media") / "source.mp4"
    video_filter = None
    font = _font()
    if font:
        video_filter = (
            f"drawtext=fontfile={font}:text='FRAME %{{n}}':x=20:y=40:"
            f"fontsize=48:fontcolor=white:box=1:boxcolor=black@0.8:boxborderw=10"
        )

    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=30",
    ]
    if video_filter:
        command += ["-vf", video_filter]
    command += [
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-g", "250", "-keyint_min", "250", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


def video_stream_duration(path) -> float:
    """Duration of the video stream alone.

    Container duration includes audio, and AAC pads to a whole frame, so it
    overstates by a few milliseconds. For asserting on cut accuracy the video
    stream is the honest number.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def frame_count(path) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return int(result.stdout.strip())


def psnr_between(first, second) -> float:
    """PSNR between two images. inf means pixel-identical."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(first), "-i", str(second),
         "-lavfi", "psnr", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for token in result.stderr.split():
        if token.startswith("average:"):
            value = token.split(":", 1)[1]
            return float("inf") if value == "inf" else float(value)
    raise AssertionError(f"could not read PSNR from ffmpeg output:\n{result.stderr}")


def extract_frame(source, destination, at: float | None = None):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if at is not None:
        command += ["-ss", f"{at:.6f}"]
    command += ["-i", str(source), "-frames:v", "1", str(destination)]
    subprocess.run(command, check=True, capture_output=True)
    return destination
