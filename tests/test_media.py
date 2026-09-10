"""Tests for the part of Clipstory that must never be approximately right.

The headline test is `test_cut_lands_on_the_exact_requested_frame`, which cuts
at a point deliberately far from any keyframe and proves the output starts on
the requested frame rather than the nearest keyframe.
"""
import subprocess

import pytest

from app.media import (
    CutVerificationError,
    MediaError,
    build_cut_command,
    cut_clip,
    probe,
)
from tests.conftest import (
    extract_frame,
    frame_count,
    psnr_between,
    requires_ffmpeg,
    video_stream_duration,
)

# The source has keyframes at 0, 10 and 20 seconds. 17.4s is 7.4 seconds past
# the nearest one, so a keyframe-snapped cut is impossible to mistake for a
# correct one.
AWKWARD_START = 17.400
CLIP_DURATION = 5.000
NEAREST_KEYFRAME = 10.000

# A re-encode of identical picture content lands well above this. Genuinely
# different frames from testsrc2 score in the low teens.
SAME_FRAME_PSNR = 30.0


class TestBuildCutCommand:
    def test_seek_is_before_the_input(self):
        command = build_cut_command("in.mp4", "out.mp4", 10.0, 5.0)
        assert command.index("-ss") < command.index("-i"), (
            "-ss must precede -i for fast seeking; after -i ffmpeg decodes the "
            "entire file up to the cut point instead"
        )

    def test_duration_is_expressed_as_t_not_to(self):
        command = build_cut_command("in.mp4", "out.mp4", 10.0, 5.0)
        assert "-t" in command
        assert "-to" not in command, (
            "-to is measured against the rebased timeline when -ss precedes -i"
        )

    def test_never_stream_copies(self):
        command = build_cut_command("in.mp4", "out.mp4", 10.0, 5.0)
        joined = " ".join(command)
        assert "-c copy" not in joined
        assert "libx264" in joined

    def test_carries_compatibility_and_streaming_flags(self):
        command = " ".join(build_cut_command("in.mp4", "out.mp4", 10.0, 5.0))
        assert "yuv420p" in command
        assert "+faststart" in command

    def test_rebases_both_stream_timelines_to_zero(self):
        command = " ".join(build_cut_command("in.mp4", "out.mp4", 10.0, 5.0))
        assert "setpts=PTS-STARTPTS" in command
        assert "asetpts=PTS-STARTPTS" in command, (
            "without these the clip timeline starts late, shifting every event "
            "in the clip and inflating the reported duration"
        )

    def test_does_not_use_avoid_negative_ts(self):
        # It runs at the muxer, after the setpts filters, and reinstates the
        # timeline offset they just removed. Measured, not theorised.
        command = " ".join(build_cut_command("in.mp4", "out.mp4", 10.0, 5.0))
        assert "avoid_negative_ts" not in command


@requires_ffmpeg
class TestProbe:
    def test_reads_source_metadata(self, marked_source):
        info = probe(marked_source)
        assert info.duration == pytest.approx(30.0, abs=0.1)
        assert (info.width, info.height) == (640, 360)
        assert info.fps == pytest.approx(25.0, abs=0.01)
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert info.variable_frame_rate is False

    def test_frame_duration_derives_from_fps(self, marked_source):
        assert probe(marked_source).frame_duration == pytest.approx(0.04, abs=1e-6)

    def test_rejects_a_file_with_no_video_stream(self, tmp_path):
        audio_only = tmp_path / "audio.m4a"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:a", "aac", str(audio_only)],
            check=True, capture_output=True,
        )
        with pytest.raises(MediaError, match="no video stream"):
            probe(audio_only)

    def test_rejects_a_file_that_is_not_video(self, tmp_path):
        junk = tmp_path / "notes.txt"
        junk.write_text("this is not a video")
        with pytest.raises(MediaError):
            probe(junk)


@requires_ffmpeg
class TestCutAccuracy:
    def test_cut_lands_on_the_exact_requested_frame(self, marked_source, tmp_path):
        """The central guarantee of this product.

        Cut 7.4 seconds away from the nearest keyframe and confirm the output's
        first frame is the source's frame at exactly 17.400s, not the keyframe
        at 10.000s that a stream copy would have snapped to.
        """
        clip = tmp_path / "clip.mp4"
        cut_clip(marked_source, clip, AWKWARD_START, CLIP_DURATION, source_fps=25.0)

        first_frame = extract_frame(clip, tmp_path / "first.png")
        at_request = extract_frame(marked_source, tmp_path / "requested.png", at=AWKWARD_START)
        at_keyframe = extract_frame(marked_source, tmp_path / "keyframe.png", at=NEAREST_KEYFRAME)

        matches_request = psnr_between(first_frame, at_request)
        matches_keyframe = psnr_between(first_frame, at_keyframe)

        assert matches_request > SAME_FRAME_PSNR, (
            f"clip does not start at the requested {AWKWARD_START}s "
            f"(PSNR {matches_request:.1f})"
        )
        assert matches_keyframe < SAME_FRAME_PSNR, (
            f"clip appears to have snapped to the keyframe at {NEAREST_KEYFRAME}s "
            f"(PSNR {matches_keyframe:.1f})"
        )

    def test_stream_copy_would_have_been_wrong(self, marked_source, tmp_path):
        """Guards the design decision itself.

        If a future change swaps in `-c copy` for speed, this test documents
        exactly what that costs: a cut in the wrong place and a clip of the
        wrong length.
        """
        copied = tmp_path / "copied.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(AWKWARD_START), "-i", str(marked_source),
             "-t", str(CLIP_DURATION), "-c", "copy",
             "-avoid_negative_ts", "make_zero", str(copied)],
            check=True, capture_output=True,
        )

        first_frame = extract_frame(copied, tmp_path / "copy_first.png")
        at_keyframe = extract_frame(marked_source, tmp_path / "kf.png", at=NEAREST_KEYFRAME)

        assert psnr_between(first_frame, at_keyframe) > SAME_FRAME_PSNR, (
            "stream copy is expected to snap back to the preceding keyframe"
        )
        assert video_stream_duration(copied) > CLIP_DURATION * 2, (
            "stream copy is expected to overshoot the requested duration badly"
        )

    def test_duration_and_frame_count_are_exact(self, marked_source, tmp_path):
        clip = tmp_path / "clip.mp4"
        cut_clip(marked_source, clip, AWKWARD_START, CLIP_DURATION, source_fps=25.0)
        assert video_stream_duration(clip) == pytest.approx(CLIP_DURATION, abs=0.04)
        assert frame_count(clip) == int(CLIP_DURATION * 25)

    @pytest.mark.parametrize("start", [0.0, 0.04, 9.96, 10.0, 17.4, 24.999])
    def test_accurate_from_any_offset(self, marked_source, tmp_path, start):
        """Accuracy must not depend on luck about where keyframes fall."""
        clip = tmp_path / f"clip_{start}.mp4"
        cut_clip(marked_source, clip, start, 2.0, source_fps=25.0)
        first_frame = extract_frame(clip, tmp_path / f"f_{start}.png")
        expected = extract_frame(marked_source, tmp_path / f"e_{start}.png", at=start)
        assert psnr_between(first_frame, expected) > SAME_FRAME_PSNR

    def test_clip_starts_its_own_timeline_at_zero(self, marked_source, tmp_path):
        clip = tmp_path / "clip.mp4"
        cut_clip(marked_source, clip, AWKWARD_START, CLIP_DURATION, source_fps=25.0)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "frame=pts_time", "-read_intervals", "%+#1",
             "-of", "csv=p=0", str(clip)],
            check=True, capture_output=True, text=True,
        )
        assert float(result.stdout.strip().rstrip(",")) == pytest.approx(0.0, abs=0.001)


@requires_ffmpeg
class TestCutVerification:
    def test_returns_probed_info_for_the_rendered_clip(self, marked_source, tmp_path):
        info = cut_clip(marked_source, tmp_path / "c.mp4", 5.0, 3.0, source_fps=25.0)
        assert info.duration == pytest.approx(3.0, abs=0.04)
        assert (info.width, info.height) == (640, 360)

    def test_requesting_past_the_end_is_caught(self, marked_source, tmp_path):
        # The source is 30s. Asking for 10s starting at 28s can only yield 2s,
        # and the verification must refuse to pass that off as complete.
        with pytest.raises(CutVerificationError, match="drift"):
            cut_clip(marked_source, tmp_path / "over.mp4", 28.0, 10.0, source_fps=25.0)

    def test_failed_verification_leaves_no_file_behind(self, marked_source, tmp_path):
        destination = tmp_path / "over.mp4"
        with pytest.raises(CutVerificationError):
            cut_clip(marked_source, destination, 28.0, 10.0, source_fps=25.0)
        assert not destination.exists(), (
            "a clip that failed verification must not be left where it could be served"
        )

    @pytest.mark.parametrize("start,duration", [(-1.0, 5.0), (0.0, 0.0), (0.0, -5.0)])
    def test_nonsensical_arguments_rejected_before_ffmpeg_runs(
        self, marked_source, tmp_path, start, duration
    ):
        with pytest.raises(ValueError):
            cut_clip(marked_source, tmp_path / "x.mp4", start, duration)

    def test_missing_source_raises_media_error(self, tmp_path):
        with pytest.raises(MediaError):
            cut_clip(tmp_path / "nope.mp4", tmp_path / "out.mp4", 0.0, 1.0)
