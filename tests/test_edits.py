"""Tests for the text-editing maths.

A bug here does not crash anything. It produces a video that is quietly wrong:
a cut half a word early, a pause that vanished, a sentence that survived when
the operator struck it out. So the rules are pinned individually.
"""
import pytest

from app.edits import (
    DEFAULT_FILLERS,
    EPSILON,
    FILLER,
    MANUAL,
    SILENCE,
    Deletion,
    EditError,
    Word,
    auto_cleanup,
    clamp_deletions,
    find_fillers,
    find_long_silences,
    keep_ranges,
    kept_duration,
    mark_words,
    merge_deletions,
    prepare,
    snap_to_silence,
    summarise,
    without_reason,
    words_from_segments,
)


def speech(*specs: tuple[float, float, str]) -> list[Word]:
    return [Word(start=s, end=e, text=t) for s, e, t in specs]


# A tidy sentence with a one-second gap in the middle of it.
SENTENCE = speech(
    (0.0, 0.5, "The"),
    (0.5, 1.0, "turning"),
    (1.0, 1.6, "point"),
    (2.6, 3.0, "was"),      # 1.0s pause before this word
    (3.0, 3.6, "pricing."),
)


class TestWordNormalisation:
    @pytest.mark.parametrize(
        "text,expected",
        [("Um,", "um"), ("UH", "uh"), ("pricing.", "pricing"),
         ("don't", "don't"), ("  so  ", "so"), ("--", "")],
    )
    def test_strips_punctuation_and_case(self, text, expected):
        assert Word(0, 1, text).normalised == expected

    def test_apostrophes_survive_inside_a_word(self):
        assert Word(0, 1, "I'm").normalised == "i'm"


class TestWordsFromSegments:
    def test_flattens_stored_segment_rows(self):
        segments = [
            {"words": [{"s": 0.0, "e": 0.5, "t": "one"}, {"s": 0.5, "e": 1.0, "t": "two"}]},
            {"words": [{"s": 1.0, "e": 1.5, "t": "three"}]},
        ]
        assert [w.text for w in words_from_segments(segments)] == ["one", "two", "three"]

    def test_tolerates_a_segment_with_no_words(self):
        assert words_from_segments([{"words": None}, {"words": []}]) == []

    def test_drops_empty_and_zero_length_words(self):
        # Whisper occasionally emits both; neither can anchor a cut.
        segments = [{"words": [
            {"s": 0.0, "e": 0.5, "t": "real"},
            {"s": 0.5, "e": 0.5, "t": "zero"},
            {"s": 0.6, "e": 0.9, "t": "   "},
        ]}]
        assert [w.text for w in words_from_segments(segments)] == ["real"]

    def test_returns_words_in_time_order(self):
        segments = [{"words": [{"s": 2.0, "e": 2.5, "t": "second"}, {"s": 0.0, "e": 0.5, "t": "first"}]}]
        assert [w.text for w in words_from_segments(segments)] == ["first", "second"]


class TestMergeDeletions:
    def test_sorts_and_leaves_disjoint_ranges_alone(self):
        merged = merge_deletions([Deletion(5, 6), Deletion(1, 2)])
        assert [(d.start, d.end) for d in merged] == [(1, 2), (5, 6)]

    def test_overlapping_ranges_become_one(self):
        merged = merge_deletions([Deletion(1, 5), Deletion(3, 8)])
        assert [(d.start, d.end) for d in merged] == [(1, 8)]

    def test_touching_ranges_become_one(self):
        merged = merge_deletions([Deletion(1, 3), Deletion(3, 5)])
        assert [(d.start, d.end) for d in merged] == [(1, 5)]

    def test_a_contained_range_is_absorbed(self):
        merged = merge_deletions([Deletion(0, 10), Deletion(3, 4)])
        assert [(d.start, d.end) for d in merged] == [(0, 10)]

    def test_zero_length_ranges_are_dropped(self):
        assert merge_deletions([Deletion(2, 2)]) == []

    def test_reason_survives_when_merged_ranges_agree(self):
        merged = merge_deletions([Deletion(1, 3, FILLER), Deletion(2, 5, FILLER)])
        assert merged[0].reason == FILLER

    def test_merging_different_reasons_yields_manual(self):
        # An operator's own cut must not be undone by "undo filler removal",
        # so the more specific claim wins.
        merged = merge_deletions([Deletion(1, 3, FILLER), Deletion(2, 5, MANUAL)])
        assert merged[0].reason == MANUAL

    def test_empty_input(self):
        assert merge_deletions([]) == []


class TestClampDeletions:
    def test_trims_a_range_that_runs_past_the_end(self):
        (clamped,) = clamp_deletions([Deletion(8, 20)], duration=10)
        assert (clamped.start, clamped.end) == (8, 10)

    def test_trims_a_negative_start(self):
        (clamped,) = clamp_deletions([Deletion(-5, 3)], duration=10)
        assert (clamped.start, clamped.end) == (0, 3)

    def test_drops_a_range_entirely_outside(self):
        assert clamp_deletions([Deletion(20, 30)], duration=10) == []

    def test_preserves_the_reason(self):
        (clamped,) = clamp_deletions([Deletion(-1, 3, SILENCE)], duration=10)
        assert clamped.reason == SILENCE


class TestSnapToSilence:
    def test_edges_move_to_the_middle_of_the_surrounding_gaps(self):
        # Delete "was" (2.6 to 3.0). The gap before it runs 1.6 to 2.6, so the
        # cut should start at 2.1. There is no gap after it, so the end holds.
        (snapped,) = snap_to_silence([Deletion(2.6, 3.0)], SENTENCE)
        assert snapped.start == pytest.approx(2.1)
        assert snapped.end == pytest.approx(3.0)

    def test_expansion_never_reaches_the_neighbouring_word(self):
        (snapped,) = snap_to_silence([Deletion(2.6, 3.0)], SENTENCE)
        assert snapped.start > 1.6, "must not touch the word that ends at 1.6"

    def test_an_edge_with_no_gap_does_not_move(self):
        # "turning" runs 0.5 to 1.0 with no silence either side.
        (snapped,) = snap_to_silence([Deletion(0.5, 1.0)], SENTENCE)
        assert (snapped.start, snapped.end) == pytest.approx((0.5, 1.0))

    def test_a_deletion_at_the_very_start_keeps_its_start(self):
        (snapped,) = snap_to_silence([Deletion(0.0, 0.5)], SENTENCE)
        assert snapped.start == 0.0

    def test_a_deletion_at_the_very_end_keeps_its_end(self):
        (snapped,) = snap_to_silence([Deletion(3.0, 3.6)], SENTENCE)
        assert snapped.end == pytest.approx(3.6)

    def test_no_words_leaves_deletions_untouched(self):
        assert snap_to_silence([Deletion(1, 2)], []) == [Deletion(1, 2)]

    def test_reason_survives_snapping(self):
        (snapped,) = snap_to_silence([Deletion(2.6, 3.0, FILLER)], SENTENCE)
        assert snapped.reason == FILLER

    def test_prepare_can_be_asked_not_to_snap(self):
        (prepared,) = prepare([Deletion(2.6, 3.0)], SENTENCE, duration=4.0, snap=False)
        assert (prepared.start, prepared.end) == pytest.approx((2.6, 3.0))


class TestKeepRanges:
    def test_a_deletion_in_the_middle_leaves_two_ranges(self):
        assert keep_ranges(10, [Deletion(4, 6)]) == [(0.0, 4.0), (6.0, 10.0)]

    def test_a_deletion_at_the_start_leaves_one_range(self):
        assert keep_ranges(10, [Deletion(0, 4)]) == [(4.0, 10.0)]

    def test_a_deletion_at_the_end_leaves_one_range(self):
        assert keep_ranges(10, [Deletion(6, 10)]) == [(0.0, 6.0)]

    def test_no_deletions_keeps_everything(self):
        assert keep_ranges(10, []) == [(0.0, 10.0)]

    def test_several_deletions_produce_the_complement(self):
        assert keep_ranges(20, [Deletion(2, 4), Deletion(8, 9), Deletion(15, 16)]) == [
            (0.0, 2.0), (4.0, 8.0), (9.0, 15.0), (16.0, 20.0)
        ]

    def test_imperceptible_slivers_are_absorbed(self):
        # Keeping 20ms would cost a whole extra segment in the filtergraph for
        # a fragment nobody can perceive.
        ranges = keep_ranges(10, [Deletion(0, 3), Deletion(3.02, 6)])
        assert ranges == [(6.0, 10.0)], "the 20ms sliver at 3.00-3.02 is not worth a segment"

    def test_deleting_everything_is_rejected(self):
        with pytest.raises(EditError, match="only"):
            keep_ranges(10, [Deletion(0, 10)])

    def test_leaving_under_a_second_is_rejected(self):
        with pytest.raises(EditError, match="restore something"):
            keep_ranges(10, [Deletion(0, 9.5)])

    def test_zero_duration_source_is_rejected(self):
        with pytest.raises(EditError, match="no duration"):
            keep_ranges(0, [])

    def test_kept_duration_is_the_sum_of_the_ranges(self):
        assert kept_duration(20, [Deletion(2, 4), Deletion(8, 9)]) == pytest.approx(17.0)


class TestMarkWords:
    def test_a_word_inside_a_deletion_is_struck(self):
        flags = mark_words(SENTENCE, [Deletion(2.6, 3.0)])
        assert flags == [False, False, False, True, False]

    def test_a_word_barely_clipped_is_not_struck(self):
        # A cut that takes 10ms off a neighbour must not strike it out.
        flags = mark_words(SENTENCE, [Deletion(0.99, 1.6)])
        assert flags[1] is False, "'turning' lost only 10ms"
        assert flags[2] is True, "'point' was removed"

    def test_a_deletion_spanning_several_words_strikes_them_all(self):
        assert mark_words(SENTENCE, [Deletion(0.0, 1.6)]) == [True, True, True, False, False]

    def test_no_deletions_strikes_nothing(self):
        assert mark_words(SENTENCE, []) == [False] * 5


class TestFindFillers:
    def test_finds_a_default_filler(self):
        words = speech((0, 0.3, "So"), (0.3, 0.6, "um"), (0.6, 1.0, "yes"))
        (deletion,) = find_fillers(words)
        assert (deletion.start, deletion.end) == pytest.approx((0.3, 0.6))
        assert deletion.reason == FILLER

    def test_matching_ignores_case_and_punctuation(self):
        words = speech((0, 0.3, "Um,"), (0.3, 0.6, "UH."))
        assert len(find_fillers(words)) == 2

    def test_never_matches_a_substring(self):
        # The bug that would quietly destroy a transcript.
        words = speech((0, 0.4, "number"), (0.4, 0.8, "software"), (0.8, 1.2, "Uhura"))
        assert find_fillers(words) == []

    def test_conversational_words_are_left_alone_by_default(self):
        # "so", "like" and "right" are usually doing real work in a sentence.
        words = speech((0, 0.3, "So"), (0.3, 0.6, "like"), (0.6, 1.0, "right"))
        assert find_fillers(words) == []
        assert {"so", "like", "right"}.isdisjoint(DEFAULT_FILLERS)

    def test_they_can_be_opted_into(self):
        words = speech((0, 0.3, "So"), (0.3, 0.6, "yes"))
        (deletion,) = find_fillers(words, vocabulary={"so"})
        assert deletion.start == 0

    def test_a_multi_word_phrase_is_removed_whole(self):
        words = speech((0, 0.3, "you"), (0.3, 0.6, "know"), (0.6, 1.0, "it"))
        (deletion,) = find_fillers(words, vocabulary={"you know"})
        assert (deletion.start, deletion.end) == pytest.approx((0.0, 0.6))

    def test_a_longer_phrase_wins_over_a_shorter_one(self):
        words = speech((0, 0.3, "you"), (0.3, 0.6, "know"))
        deletions = find_fillers(words, vocabulary={"you know", "know"})
        assert len(deletions) == 1 and deletions[0].start == 0.0

    def test_empty_vocabulary_finds_nothing(self):
        assert find_fillers(SENTENCE, vocabulary=set()) == []

    def test_consecutive_fillers_are_found_separately(self):
        words = speech((0, 0.3, "um"), (0.3, 0.6, "uh"))
        assert len(find_fillers(words)) == 2


class TestFindLongSilences:
    def test_a_long_gap_is_shortened_not_removed(self):
        # The 1.0s gap from 1.6 to 2.6 should be trimmed to 0.4s, so 0.6s goes.
        deletions = find_long_silences(SENTENCE, duration=3.6)
        assert len(deletions) == 1
        deletion = deletions[0]
        assert deletion.duration == pytest.approx(0.6)
        assert deletion.reason == SILENCE

    def test_the_remaining_pause_sits_either_side_of_the_cut(self):
        (deletion,) = find_long_silences(SENTENCE, duration=3.6)
        assert deletion.start == pytest.approx(1.8), "0.2s of silence kept before the cut"
        assert deletion.end == pytest.approx(2.4), "0.2s of silence kept after it"
        surviving_pause = (deletion.start - 1.6) + (2.6 - deletion.end)
        assert surviving_pause == pytest.approx(0.4), "what is left equals the target"

    def test_short_gaps_are_left_alone(self):
        words = speech((0, 0.5, "a"), (0.9, 1.4, "b"))  # 0.4s gap
        assert find_long_silences(words, duration=1.4) == []

    def test_a_slow_start_is_trimmed(self):
        words = speech((5.0, 5.5, "hello"))
        (deletion,) = find_long_silences(words, duration=6.0)
        assert deletion.start == pytest.approx(0.2) and deletion.end == pytest.approx(4.8)

    def test_a_trailing_dead_end_is_trimmed(self):
        words = speech((0.0, 0.5, "bye"))
        (deletion,) = find_long_silences(words, duration=5.5)
        assert deletion.start == pytest.approx(0.7) and deletion.end == pytest.approx(5.3)

    def test_thresholds_are_configurable(self):
        words = speech((0, 0.5, "a"), (1.0, 1.5, "b"))  # 0.5s gap
        assert find_long_silences(words, 1.5, threshold=0.3, target=0.1)
        assert find_long_silences(words, 1.5, threshold=2.0) == []

    def test_a_target_at_or_above_the_threshold_is_rejected(self):
        with pytest.raises(EditError, match="shorter than the threshold"):
            find_long_silences(SENTENCE, 3.6, threshold=0.5, target=0.5)

    def test_a_silent_video_is_trimmed_to_the_target(self):
        (deletion,) = find_long_silences([], duration=10.0)
        assert deletion.duration == pytest.approx(9.6)


class TestAutoCleanup:
    def test_runs_both_passes(self):
        words = speech((0, 0.3, "um"), (0.3, 0.6, "yes"), (2.0, 2.4, "quite"))
        reasons = {d.reason for d in auto_cleanup(words, duration=2.4)}
        assert reasons == {FILLER, SILENCE}

    def test_each_pass_can_be_switched_off(self):
        words = speech((0, 0.3, "um"), (0.3, 0.6, "yes"), (2.0, 2.4, "quite"))
        assert all(d.reason == FILLER for d in auto_cleanup(words, 2.4, shorten_silences=False))
        assert all(d.reason == SILENCE for d in auto_cleanup(words, 2.4, remove_fillers=False))

    def test_both_off_produces_nothing(self):
        assert auto_cleanup(SENTENCE, 3.6, remove_fillers=False, shorten_silences=False) == []

    def test_cleanup_output_survives_prepare(self):
        words = speech((0, 0.3, "um"), (0.3, 0.6, "yes"), (2.0, 2.4, "quite"))
        prepared = prepare(auto_cleanup(words, 2.4), words, 2.4)
        assert prepared == sorted(prepared, key=lambda d: d.start)
        for first, second in zip(prepared, prepared[1:]):
            assert first.end <= second.start + EPSILON, "prepare must leave them disjoint"


class TestSummaryAndUndo:
    def test_counts_and_seconds_per_reason(self):
        summary = summarise([Deletion(0, 1, FILLER), Deletion(2, 3, FILLER), Deletion(5, 7, SILENCE)])
        assert summary[FILLER] == {"count": 2, "seconds": 2.0}
        assert summary[SILENCE] == {"count": 1, "seconds": 2.0}
        assert summary["total"] == {"count": 3, "seconds": 4.0}

    def test_empty_summary(self):
        assert summarise([])["total"] == {"count": 0, "seconds": 0.0}

    def test_undo_one_category_keeps_manual_edits(self):
        deletions = [Deletion(0, 1, FILLER), Deletion(2, 3, MANUAL), Deletion(5, 6, SILENCE)]
        remaining = without_reason(deletions, FILLER)
        assert [d.reason for d in remaining] == [MANUAL, SILENCE]


class TestJsonRoundTrip:
    def test_survives_a_round_trip(self):
        original = Deletion(1.2345, 6.7891, FILLER)
        restored = Deletion.from_json(original.to_json())
        assert restored.reason == FILLER
        assert restored.start == pytest.approx(1.2345, abs=0.001)

    def test_a_missing_reason_defaults_to_manual(self):
        assert Deletion.from_json({"start": 1, "end": 2}).reason == MANUAL


class TestScale:
    def test_a_cleanup_sized_edit_prepares_quickly(self):
        # A one-hour interview is tens of thousands of words and a cleanup pass
        # produces hundreds of deletions. This must not be quadratic.
        words = [Word(i * 0.4, i * 0.4 + 0.3, "um" if i % 20 == 0 else "word")
                 for i in range(9000)]
        duration = words[-1].end
        deletions = auto_cleanup(words, duration)
        assert len(deletions) > 400
        prepared = prepare(deletions, words, duration)
        ranges = keep_ranges(duration, prepared)
        assert len(ranges) > 400
        assert sum(e - s for s, e in ranges) < duration
