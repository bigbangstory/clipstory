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


# ------------------------------------------------------------ providers ----
# The suite must never load Whisper weights or call a language model. These
# fakes stand in for both, and the DB-backed tests inject them.

from app.suggest import SuggestedClip, SuggestionProvider, SuggestionResponse  # noqa: E402
from app.transcription import TranscriptSegment, TranscriptionProvider, Word  # noqa: E402


class FakeTranscription(TranscriptionProvider):
    """Returns a fixed transcript: one 5-second segment every 5 seconds."""

    name = "fake"
    enabled = True

    def __init__(self, duration: float = 30.0, step: float = 5.0):
        self.duration, self.step = duration, step
        self.calls = 0

    def transcribe(self, audio_path):
        self.calls += 1
        segments = []
        t = 0.0
        while t < self.duration:
            end = min(t + self.step, self.duration)
            words = [Word(t, t + 1.0, "alpha"), Word(t + 1.0, t + 2.0, "beta"), Word(t + 2.0, end, "gamma")]
            segments.append(TranscriptSegment(len(segments), t, end, f"segment {len(segments)} alpha beta gamma", words))
            t = end
        return segments, "en"


class FakeSuggestions(SuggestionProvider):
    """Proposes fixed segment ranges, or raises, as the test dictates."""

    name = "fake"

    def __init__(self, clips=None, fail: str | None = None):
        self.clips = clips if clips is not None else [
            SuggestedClip(start_segment=1, end_segment=2, title="Founder origin story", reason="hook"),
            SuggestedClip(start_segment=4, end_segment=4, title="Closing line", reason="resolves"),
        ]
        self.fail = fail
        self.calls = 0

    def propose(self, transcript, target_count, segment_count):
        from app.suggest import SuggestionError

        self.calls += 1
        if self.fail:
            raise SuggestionError(self.fail)
        return SuggestionResponse(clips=self.clips)


@pytest.fixture
def fake_providers():
    """Install fake transcription + suggestion providers for one test."""
    from app import suggest, transcription

    t, s = FakeTranscription(), FakeSuggestions()
    transcription.set_provider(t)
    suggest.set_provider(s)
    try:
        yield t, s
    finally:
        transcription.set_provider(None)
        suggest.set_provider(None)


@pytest.fixture(scope="module")
def database():
    """A clean schema in the test database. Skips without TEST_DATABASE_URL."""
    import os

    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL not set")
    from app import db

    db.wait_for_database()
    with db.connection() as conn:
        conn.execute(
            "DROP TABLE IF EXISTS transcript_segments, clips, jobs, login_tokens, invites, users CASCADE"
        )
    db.apply_schema()
    yield db
