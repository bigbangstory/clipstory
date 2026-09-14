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


class TestFrameAlignment:
    """Alignment is not cosmetic: get it wrong and the edit is silently long.

    Video can only be cut on a frame, so a boundary that lands on a frame's own
    timestamp rounds unpredictably and keeps an extra frame about half the time.
    Frame indices are used as the domain throughout so that the count, the join
    positions and the select expression all come from one rounding.
    """

    def test_ranges_become_half_open_frame_indices(self):
        from app.media import align_ranges_to_frames

        assert align_ranges_to_frames([(0.0, 2.0), (10.0, 13.0)], 25.0) == [(0, 50), (250, 325)]

    def test_awkward_boundaries_round_to_the_nearest_frame(self):
        from app.media import align_ranges_to_frames

        # At 25fps a frame is 40ms, so 1.987 rounds to frame 50 and 3.013 to 75.
        assert align_ranges_to_frames([(0.0, 1.987), (3.013, 5.0)], 25.0) == [(0, 50), (75, 125)]

    def test_the_frame_count_is_exact_not_re_rounded(self):
        from app.media import align_ranges_to_frames, expected_frame_count

        # Deriving the count by rounding seconds a second time gives 124 here,
        # because Python rounds halves to even and 324.5 goes down.
        ranges = align_ranges_to_frames([(0.0, 2.0), (10.0, 13.0)], 25.0)
        assert expected_frame_count(ranges) == 125

    def test_boundaries_are_placed_between_frames_not_on_them(self):
        from app.media import align_ranges_to_frames, frame_ranges_to_seconds

        seconds = frame_ranges_to_seconds(align_ranges_to_frames([(0.0, 2.0)], 25.0), 25.0)
        # Half a frame before frame 0 and before frame 50, so `t >= start` and
        # `t < end` cannot land ambiguously on a frame's own timestamp.
        assert seconds == [(-0.02, 1.98)]

    def test_a_sub_frame_deletion_merges_its_neighbours(self):
        from app.media import align_ranges_to_frames

        # A 15ms gap cannot exist in 40ms frames, so this is one segment rather
        # than a join that removes nothing.
        assert align_ranges_to_frames([(0.0, 2.0), (2.015, 4.0)], 25.0) == [(0, 100)]

    def test_a_range_shorter_than_a_frame_is_dropped(self):
        from app.media import align_ranges_to_frames

        assert align_ranges_to_frames([(0.0, 0.005)], 25.0) == []

    def test_zero_fps_falls_back_rather_than_dividing_by_zero(self):
        from app.media import align_ranges_to_frames

        assert align_ranges_to_frames([(0.0, 2.0)], 0.0) == [(0, 60)]  # FALLBACK_FPS


class TestAudioLock:
    """Audio is selected in whole packets, video in whole frames. Without a
    sample rate that divides evenly into a frame, those two granularities drift
    apart across hundreds of cuts. Measured here: 54ms over 281 segments."""

    def test_integer_rates_lock_at_48k(self):
        from fractions import Fraction

        from app.media import audio_lock

        assert audio_lock(Fraction(25)) == (48000, 1920)
        assert audio_lock(Fraction(30)) == (48000, 1600)

    def test_ntsc_rates_need_a_different_rate_to_divide_exactly(self):
        from fractions import Fraction

        from app.media import audio_lock

        # 48000/29.97 is 1601.6 samples. Rounding that leaves a systematic bias
        # that still drifts; 60000 divides exactly.
        assert audio_lock(Fraction(30000, 1001)) == (60000, 2002)
        assert audio_lock(Fraction(24000, 1001)) == (48000, 2002)

    def test_every_lock_is_a_whole_number_of_samples_per_frame(self):
        from fractions import Fraction

        from app.media import audio_lock

        for fps in (Fraction(24), Fraction(25), Fraction(30), Fraction(50), Fraction(60),
                    Fraction(30000, 1001), Fraction(24000, 1001), Fraction(60000, 1001)):
            rate, per_frame = audio_lock(fps)
            assert Fraction(rate, per_frame) == fps, f"{fps} does not lock exactly"


class TestBuildEditFilters:
    def test_selects_rather_than_trimming_and_concatenating(self):
        from app.media import align_ranges_to_frames, build_edit_filters

        # trim+concat holds the whole source span in memory: measured at 4.40 GB
        # for 60s of 1080p against 0.19 GB for select. On an hour it would be
        # killed. select's cost is flat in both source length and cut count.
        video, audio = build_edit_filters(
            align_ranges_to_frames([(0.0, 2.0), (10.0, 13.0)], 25.0), 25.0
        )
        assert video.startswith("select=")
        assert "trim=" not in video and "concat=" not in video
        assert audio.count("aselect=") == 1

    def test_uses_gte_and_lt_never_between(self):
        from app.media import align_ranges_to_frames, build_edit_filters

        # between() is inclusive at both ends and keeps one extra frame per
        # segment. Measured: 200 segments gave 2600 frames for a 2400-frame
        # request and 6.4s of audio/video desync, with no error anywhere.
        video, _ = build_edit_filters(align_ranges_to_frames([(0.0, 2.0)], 25.0), 25.0)
        assert "between(" not in video
        assert "gte(t," in video and "lt(t," in video

    def test_renumbers_frames_at_a_constant_rate(self):
        from app.media import align_ranges_to_frames, build_edit_filters

        # PTS-STARTPTS only rebases the first frame and leaves the holes in.
        video, audio = build_edit_filters(align_ranges_to_frames([(0.0, 2.0)], 25.0), 25.0)
        assert "setpts=N/(25*TB)" in video
        assert "asetpts=N/SR/TB" in audio

    def test_audio_is_repacketised_to_one_frame_per_chunk_when_locked(self):
        from app.media import align_ranges_to_frames, audio_lock, build_edit_filters
        from fractions import Fraction

        _, audio = build_edit_filters(
            align_ranges_to_frames([(0.0, 2.0)], 25.0), 25.0, lock=audio_lock(Fraction(25))
        )
        assert "asetnsamples=n=1920:p=0" in audio
        assert audio.index("asetnsamples") < audio.index("aselect"), (
            "the repacketising has to happen before the selection, or it locks nothing"
        )

    def test_a_non_standard_lock_rate_is_resampled_back_for_the_encoder(self):
        from fractions import Fraction

        from app.media import align_ranges_to_frames, audio_lock, build_edit_filters

        _, audio = build_edit_filters(
            align_ranges_to_frames([(0.0, 2.0)], 30000 / 1001),
            30000 / 1001,
            lock=audio_lock(Fraction(30000, 1001)),
        )
        assert audio.startswith("aresample=60000")
        assert audio.endswith("aresample=48000"), "AAC needs a rate it supports"

    def test_joins_get_a_fade_positioned_in_output_time(self):
        from app.media import align_ranges_to_frames, build_edit_filters

        # One join, after 2.0s of kept material, so the ramp belongs at 2.0s in
        # the output rather than anywhere in the source.
        _, audio = build_edit_filters(
            align_ranges_to_frames([(0.0, 2.0), (10.0, 13.0)], 25.0), 25.0
        )
        assert "afade=t=out:st=1.995000" in audio
        assert "afade=t=in:st=2.000000" in audio

    def test_a_single_segment_needs_no_fades(self):
        from app.media import align_ranges_to_frames, build_edit_filters

        _, audio = build_edit_filters(align_ranges_to_frames([(0.0, 2.0)], 25.0), 25.0)
        assert "afade" not in audio

    def test_a_silent_source_produces_no_audio_chain(self):
        from app.media import align_ranges_to_frames, build_edit_filters

        video, audio = build_edit_filters(
            align_ranges_to_frames([(0.0, 2.0)], 25.0), 25.0, has_audio=False
        )
        assert audio is None and video

    def test_an_empty_edit_is_refused(self):
        from app.media import build_edit_filters

        with pytest.raises(ValueError, match="at least one range"):
            build_edit_filters([], 25.0)

    def test_an_absurd_segment_count_is_refused(self):
        from app.media import MAX_EDIT_SEGMENTS, build_edit_filters

        with pytest.raises(ValueError, match="over the"):
            build_edit_filters([(i * 10, i * 10 + 5) for i in range(MAX_EDIT_SEGMENTS + 1)], 25.0)


@requires_ffmpeg
class TestRenderEdit:
    def test_removes_the_deleted_span_and_keeps_the_rest(self, marked_source, tmp_path):
        from app.media import render_edit

        out = tmp_path / "edited.mp4"
        info = render_edit(marked_source, out, [(0.0, 10.0), (15.0, 30.0)], source_fps=25.0)
        assert info.duration == pytest.approx(25.0, abs=0.04)
        assert video_stream_duration(out) == pytest.approx(25.0, abs=0.04)
        assert frame_count(out) == 25 * 25

    def test_the_frame_after_a_join_is_the_right_source_frame(self, marked_source, tmp_path):
        """The real proof. Duration alone cannot tell you *where* the join landed.

        The source has its frame number burned in, so comparing the frame just
        after the join against the source frame at 15.000s shows the edit
        resumed at exactly the right moment, not a few frames either side.
        """
        from app.media import render_edit

        out = tmp_path / "edited.mp4"
        render_edit(marked_source, out, [(0.0, 10.0), (15.0, 30.0)], source_fps=25.0)

        after_join = extract_frame(out, tmp_path / "after.png", at=10.0)
        correct = extract_frame(marked_source, tmp_path / "correct.png", at=15.0)
        wrong = extract_frame(marked_source, tmp_path / "wrong.png", at=10.0)

        assert psnr_between(after_join, correct) > SAME_FRAME_PSNR, (
            "playback did not resume at 15.000s, where the deletion ends"
        )
        assert psnr_between(after_join, wrong) < SAME_FRAME_PSNR, (
            "the deleted span is still present"
        )

    def test_the_start_of_the_edit_is_still_the_start_of_the_source(self, marked_source, tmp_path):
        from app.media import render_edit

        out = tmp_path / "edited.mp4"
        render_edit(marked_source, out, [(0.0, 10.0), (15.0, 30.0)], source_fps=25.0)
        assert psnr_between(
            extract_frame(out, tmp_path / "a.png"),
            extract_frame(marked_source, tmp_path / "b.png", at=0.0),
        ) > SAME_FRAME_PSNR

    def test_several_deletions_all_land(self, marked_source, tmp_path):
        from app.media import render_edit

        out = tmp_path / "edited.mp4"
        ranges = [(0.0, 5.0), (8.0, 12.0), (20.0, 24.0)]
        info = render_edit(marked_source, out, ranges, source_fps=25.0)
        assert info.duration == pytest.approx(13.0, abs=0.04)
        # After the second join, playback must resume at 20.000s.
        assert psnr_between(
            extract_frame(out, tmp_path / "j.png", at=9.0),
            extract_frame(marked_source, tmp_path / "k.png", at=20.0),
        ) > SAME_FRAME_PSNR

    def test_unaligned_input_is_aligned_rather_than_trusted(self, marked_source, tmp_path):
        from app.media import render_edit

        # Deliberately off-frame boundaries. Alignment is what keeps the result
        # equal to the request; without it this would come out long.
        out = tmp_path / "edited.mp4"
        info = render_edit(marked_source, out, [(0.0, 9.987), (15.013, 30.0)], source_fps=25.0)
        assert info.duration == pytest.approx(25.0, abs=0.04)

    def test_keeping_everything_reproduces_the_source_duration(self, marked_source, tmp_path):
        from app.media import render_edit

        out = tmp_path / "whole.mp4"
        info = render_edit(marked_source, out, [(0.0, 30.0)], source_fps=25.0)
        assert info.duration == pytest.approx(30.0, abs=0.04)

    def test_an_empty_edit_is_refused_before_ffmpeg_runs(self, marked_source, tmp_path):
        from app.media import render_edit

        with pytest.raises(ValueError, match="at least one range"):
            render_edit(marked_source, tmp_path / "x.mp4", [])

    def test_an_edit_finer_than_a_frame_is_refused(self, marked_source, tmp_path):
        from app.media import render_edit

        with pytest.raises(ValueError, match="frame alignment"):
            render_edit(marked_source, tmp_path / "x.mp4", [(1.0, 1.005)], source_fps=25.0)

    def test_a_range_past_the_end_fails_verification_and_leaves_no_file(
        self, marked_source, tmp_path
    ):
        from app.media import CutVerificationError, render_edit

        out = tmp_path / "over.mp4"
        # The frame-count check catches this before the duration check does,
        # and says exactly how many frames are missing rather than a drift in
        # milliseconds.
        with pytest.raises(CutVerificationError, match="were expected from"):
            render_edit(marked_source, out, [(0.0, 5.0), (25.0, 60.0)], source_fps=25.0)
        assert not out.exists(), "a clip that failed verification must not be served"

    def test_the_filtergraph_is_cleaned_up_on_success(self, marked_source, tmp_path):
        from app.media import render_edit

        out = tmp_path / "edited.mp4"
        render_edit(marked_source, out, [(0.0, 10.0), (15.0, 30.0)], source_fps=25.0)
        assert not out.with_suffix(".filtergraph.txt").exists()

    def test_output_timeline_starts_at_zero(self, marked_source, tmp_path):
        import subprocess

        from app.media import render_edit

        out = tmp_path / "edited.mp4"
        render_edit(marked_source, out, [(5.0, 10.0), (15.0, 20.0)], source_fps=25.0)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=start_time", "-of", "csv=p=0", str(out)],
            check=True, capture_output=True, text=True,
        )
        assert float(result.stdout.strip().rstrip(",")) == pytest.approx(0.0, abs=0.001)
