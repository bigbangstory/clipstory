"""Tests for the LLM suggestion layer.

The point of these tests is not that the model gives good answers; that is a
judgement call on real content. It is that a *bad* answer cannot become a wrong
cut. Every case below is one the naive design (asking the model for seconds)
would have turned into a silently misplaced clip.
"""
import pytest

from app.suggest import (
    SuggestedClip,
    SuggestionResponse,
    format_transcript,
    resolve,
    to_cut_list,
)
from app.transcription import TranscriptSegment


def segments(count: int = 5, length: float = 10.0) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            index=i,
            start=i * length,
            end=(i + 1) * length,
            text=f"sentence number {i}",
        )
        for i in range(count)
    ]


class TestResolveRejectsHallucinations:
    def test_timestamps_come_from_the_transcript_not_the_model(self):
        segs = segments()
        (result,) = resolve(
            [SuggestedClip(start_segment=1, end_segment=2, title="A", reason="r")], segs
        )
        # Exactly the boundaries Whisper measured, not anything the model said.
        assert result.start == segs[1].start == 10.0
        assert result.end == segs[2].end == 30.0

    def test_segment_index_that_does_not_exist_is_dropped(self):
        # The naive design's failure mode: a confident number for a moment that
        # is not there. Here it cannot resolve, so it disappears.
        assert resolve(
            [SuggestedClip(start_segment=999, end_segment=1000, title="Ghost", reason="r")],
            segments(),
        ) == []

    def test_negative_index_is_dropped(self):
        assert resolve(
            [SuggestedClip(start_segment=-1, end_segment=2, title="Bad", reason="r")],
            segments(),
        ) == []

    def test_one_bad_suggestion_does_not_discard_the_good_ones(self):
        results = resolve(
            [
                SuggestedClip(start_segment=0, end_segment=1, title="Good", reason="r"),
                SuggestedClip(start_segment=500, end_segment=501, title="Ghost", reason="r"),
                SuggestedClip(start_segment=3, end_segment=4, title="Also good", reason="r"),
            ],
            segments(),
        )
        assert [r.title for r in results] == ["Good", "Also good"]

    def test_backwards_range_is_dropped(self):
        assert resolve(
            [SuggestedClip(start_segment=4, end_segment=0, title="Reversed", reason="r")],
            segments(),
        ) == []

    def test_single_segment_clip_is_valid(self):
        (result,) = resolve(
            [SuggestedClip(start_segment=2, end_segment=2, title="One", reason="r")],
            segments(),
        )
        assert (result.start, result.end) == (20.0, 30.0)

    def test_empty_suggestion_list_is_fine(self):
        assert resolve([], segments()) == []

    def test_empty_transcript_drops_everything(self):
        assert resolve(
            [SuggestedClip(start_segment=0, end_segment=1, title="X", reason="r")], []
        ) == []


class TestResolveOrdering:
    def test_results_are_sorted_by_time_whatever_order_the_model_used(self):
        results = resolve(
            [
                SuggestedClip(start_segment=3, end_segment=4, title="Late", reason="r"),
                SuggestedClip(start_segment=0, end_segment=1, title="Early", reason="r"),
            ],
            segments(),
        )
        assert [r.title for r in results] == ["Early", "Late"]

    def test_overlapping_suggestions_are_deduped(self):
        # The prompt forbids overlaps; this enforces it rather than trusting it.
        results = resolve(
            [
                SuggestedClip(start_segment=0, end_segment=3, title="Wide", reason="r"),
                SuggestedClip(start_segment=2, end_segment=4, title="Overlapping", reason="r"),
            ],
            segments(),
        )
        assert [r.title for r in results] == ["Wide"]

    def test_adjacent_non_overlapping_suggestions_both_survive(self):
        results = resolve(
            [
                SuggestedClip(start_segment=0, end_segment=1, title="First", reason="r"),
                SuggestedClip(start_segment=2, end_segment=3, title="Second", reason="r"),
            ],
            segments(),
        )
        assert len(results) == 2


class TestPromptFormatting:
    def test_transcript_is_numbered_so_the_model_can_only_cite_real_segments(self):
        rendered = format_transcript(segments(3))
        assert "[0]" in rendered and "[1]" in rendered and "[2]" in rendered
        assert "sentence number 1" in rendered

    def test_each_line_carries_a_readable_timestamp(self):
        assert "(00:00:10)" in format_transcript(segments(3))

    def test_schema_forbids_the_model_returning_raw_seconds(self):
        fields = set(SuggestedClip.model_fields)
        assert fields == {"start_segment", "end_segment", "title", "reason"}
        assert not any("second" in f for f in fields), (
            "the model must never be given a field it can put a raw timestamp in"
        )


class TestCutListRendering:
    def test_renders_as_parseable_cut_lines(self):
        from app.timestamps import parse_cut_list

        suggestions = resolve(
            [
                SuggestedClip(start_segment=0, end_segment=1, title="Opening hook", reason="r"),
                SuggestedClip(start_segment=3, end_segment=4, title="Closing line", reason="r"),
            ],
            segments(),
        )
        text = to_cut_list(suggestions)
        # The output must survive the same parser as anything hand-typed.
        parsed = parse_cut_list(text)
        assert len(parsed) == 2
        assert parsed[0].label == "Opening hook"
        assert parsed[0].start == 0.0

    def test_empty_suggestions_render_as_empty_text(self):
        assert to_cut_list([]) == ""

    def test_output_is_commented_so_it_reads_as_a_proposal(self):
        suggestions = resolve(
            [SuggestedClip(start_segment=0, end_segment=1, title="X", reason="r")],
            segments(),
        )
        assert to_cut_list(suggestions).startswith("#")


class TestSuggestionResponseSchema:
    def test_parses_a_well_formed_model_response(self):
        response = SuggestionResponse.model_validate(
            {"clips": [{"start_segment": 0, "end_segment": 2,
                        "title": "T", "reason": "R"}]}
        )
        assert response.clips[0].start_segment == 0

    def test_rejects_a_response_missing_required_fields(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SuggestionResponse.model_validate({"clips": [{"start_segment": 0}]})
