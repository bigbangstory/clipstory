import pytest

from app.timestamps import (
    CutRange,
    TimestampError,
    find_overlaps,
    format_timestamp,
    parse_cut_list,
    parse_timestamp,
)


class TestParseTimestamp:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("00:04:17", 257.0),
            ("0:4:17", 257.0),
            ("04:17", 257.0),
            ("4:17", 257.0),
            ("257", 257.0),
            ("257.0", 257.0),
            ("00:04:17.500", 257.5),
            ("04:17.5", 257.5),
            ("0", 0.0),
            ("00:00:00", 0.0),
            ("01:23:45", 5025.0),
            ("99:00:00", 356400.0),  # hours are not capped at 24
        ],
    )
    def test_accepted_forms(self, text, expected):
        assert parse_timestamp(text) == expected

    def test_two_parts_mean_minutes_and_seconds_not_hours_and_minutes(self):
        # The single most dangerous ambiguity in the whole parser. "04:17"
        # must be 4m17s, never 4h17m, or every cut lands hours away.
        assert parse_timestamp("04:17") == 257.0
        assert parse_timestamp("04:17") != 4 * 3600 + 17 * 60

    def test_surrounding_whitespace_ignored(self):
        assert parse_timestamp("  00:04:17  ") == 257.0

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "abc", "00:04:17:22", "1:70", "00:99:00", "-5", "4:17.", "1e3"],
    )
    def test_rejected_forms(self, text):
        with pytest.raises(ValueError):
            parse_timestamp(text)


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "00:00:00.000"),
            (257, "00:04:17.000"),
            (257.5, "00:04:17.500"),
            (5025, "01:23:45.000"),
            (3600, "01:00:00.000"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert format_timestamp(seconds) == expected

    def test_rounding_does_not_produce_1000_milliseconds(self):
        assert format_timestamp(1.9999) == "00:00:02.000"

    def test_round_trips_with_parser(self):
        for seconds in (0, 1.5, 257.25, 5025.125):
            assert parse_timestamp(format_timestamp(seconds)) == seconds

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            format_timestamp(-1)


class TestParseCutList:
    def test_all_documented_forms_agree(self):
        ranges = parse_cut_list(
            """
            00:04:17 - 00:05:22
            00:04:17 -> 00:05:22
            04:17 to 05:22
            257, 322
            """
        )
        assert len(ranges) == 4
        assert all(r.start == 257.0 and r.end == 322.0 for r in ranges)

    def test_blank_lines_and_comments_ignored(self):
        ranges = parse_cut_list(
            """
            # first section
            00:00:10 - 00:00:20

            # second section
            00:00:30 - 00:00:40
            """
        )
        assert [r.sequence for r in ranges] == [1, 2]

    def test_label_is_captured_and_stripped(self):
        (clip,) = parse_cut_list("00:12:03 - 00:13:40 |  Founder origin story  ")
        assert clip.label == "Founder origin story"

    def test_empty_label_becomes_none(self):
        (clip,) = parse_cut_list("00:12:03 - 00:13:40 |   ")
        assert clip.label is None

    def test_sequence_follows_paste_order_not_time_order(self):
        # The operator's order is the deliverable order. A later moment pasted
        # first is still clip 01.
        ranges = parse_cut_list("00:10:00 - 00:11:00\n00:01:00 - 00:02:00")
        assert [r.sequence for r in ranges] == [1, 2]
        assert ranges[0].start == 600.0

    def test_line_numbers_survive_blank_lines(self):
        ranges = parse_cut_list("\n\n00:00:10 - 00:00:20\n")
        assert ranges[0].line_number == 3

    def test_duration_is_computed(self):
        (clip,) = parse_cut_list("00:00:10 - 00:01:15")
        assert clip.duration == 65.0


class TestValidation:
    def test_start_after_end_rejected(self):
        with pytest.raises(TimestampError) as exc:
            parse_cut_list("00:05:00 - 00:04:00")
        assert "must be before end" in exc.value.message

    def test_zero_length_rejected(self):
        with pytest.raises(TimestampError):
            parse_cut_list("00:05:00 - 00:05:00")

    def test_clip_under_one_second_rejected(self):
        with pytest.raises(TimestampError) as exc:
            parse_cut_list("00:00:10.000 - 00:00:10.500")
        assert "minimum" in exc.value.message

    def test_exactly_one_second_accepted(self):
        (clip,) = parse_cut_list("00:00:10 - 00:00:11")
        assert clip.duration == 1.0

    def test_end_past_source_duration_rejected(self):
        with pytest.raises(TimestampError) as exc:
            parse_cut_list("00:00:10 - 00:10:00", source_duration=120.0)
        assert "past the end" in exc.value.message

    def test_end_exactly_at_source_duration_accepted(self):
        (clip,) = parse_cut_list("00:00:10 - 00:02:00", source_duration=120.0)
        assert clip.end == 120.0

    def test_duration_unchecked_when_not_supplied(self):
        # Used before the video has been probed.
        parse_cut_list("00:00:10 - 10:00:00")

    def test_unparseable_line_names_its_line_number(self):
        with pytest.raises(TimestampError) as exc:
            parse_cut_list("00:00:10 - 00:00:20\nnonsense here\n00:00:30 - 00:00:40")
        assert exc.value.line_number == 2
        assert "nonsense here" in exc.value.line

    def test_missing_separator_rejected(self):
        with pytest.raises(TimestampError) as exc:
            parse_cut_list("00:00:10 00:00:20")
        assert "separator" in exc.value.message

    def test_empty_input_rejected(self):
        with pytest.raises(ValueError):
            parse_cut_list("\n# only a comment\n\n")

    def test_one_bad_line_blocks_the_whole_job(self):
        # Partial success would render some clips and silently drop others.
        with pytest.raises(TimestampError):
            parse_cut_list("00:00:10 - 00:00:20\n00:05:00 - 00:04:00")


class TestOverlaps:
    def test_no_overlap_for_sequential_ranges(self):
        assert find_overlaps(parse_cut_list("00:00:00 - 00:00:10\n00:00:10 - 00:00:20")) == []

    def test_overlap_detected(self):
        ranges = parse_cut_list("00:00:00 - 00:00:30\n00:00:20 - 00:00:40")
        assert len(find_overlaps(ranges)) == 1

    def test_overlap_detected_regardless_of_paste_order(self):
        ranges = parse_cut_list("00:00:20 - 00:00:40\n00:00:00 - 00:00:30")
        assert len(find_overlaps(ranges)) == 1

    def test_fully_contained_range_is_an_overlap(self):
        ranges = parse_cut_list("00:00:00 - 00:01:00\n00:00:10 - 00:00:20")
        assert len(find_overlaps(ranges)) == 1

    def test_overlaps_are_warnings_not_errors(self):
        # parse_cut_list must succeed; find_overlaps only reports.
        ranges = parse_cut_list("00:00:00 - 00:00:30\n00:00:20 - 00:00:40")
        assert len(ranges) == 2
