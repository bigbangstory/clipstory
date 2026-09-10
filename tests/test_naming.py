import pytest

from app.naming import clip_filename, sequence_width, slugify, source_slug, zip_filename


class TestSlugify:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Founder origin story", "founder-origin-story"),
            ("  spaced  out  ", "spaced-out"),
            ("Already-Hyphenated", "already-hyphenated"),
            ("symbols!@#$%^&*()here", "symbols-here"),
            ("MiXeD CaSe", "mixed-case"),
            ("multiple---hyphens", "multiple-hyphens"),
        ],
    )
    def test_basic_cases(self, text, expected):
        assert slugify(text) == expected

    def test_accents_reduce_to_base_letters(self):
        assert slugify("Café Señor") == "cafe-senor"

    def test_non_latin_falls_back_rather_than_producing_empty(self):
        assert slugify("日本語") == "clip"

    def test_empty_input_falls_back(self):
        assert slugify("") == "clip"
        assert slugify("!!!") == "clip"

    def test_truncates_without_leaving_a_trailing_hyphen(self):
        result = slugify("a" * 40 + " " + "b" * 40)
        assert len(result) <= 60
        assert not result.endswith("-")


class TestSourceSlug:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("Podcast Ep12.mp4", "podcast-ep12"),
            ("podcast.ep12.final.mov", "podcast-ep12-final"),
            ("no_extension", "no-extension"),
            ("/some/path/Interview FINAL.mkv", "interview-final"),
        ],
    )
    def test_extension_and_path_removed(self, filename, expected):
        assert source_slug(filename) == expected


class TestSequenceWidth:
    @pytest.mark.parametrize("total,width", [(1, 2), (9, 2), (10, 2), (99, 2), (100, 3), (1000, 4)])
    def test_widens_past_99(self, total, width):
        assert sequence_width(total) == width

    def test_zero_clips_does_not_crash(self):
        assert sequence_width(0) == 2


class TestClipFilename:
    def test_basic_pattern(self):
        assert clip_filename("Podcast Ep12.mp4", 1, 10) == "podcast-ep12_clip_01.mp4"

    def test_label_is_appended(self):
        assert (
            clip_filename("Podcast Ep12.mp4", 3, 10, "Founder origin story")
            == "podcast-ep12_clip_03_founder-origin-story.mp4"
        )

    def test_padding_widens_for_large_batches(self):
        assert clip_filename("x.mp4", 7, 150) == "x_clip_007.mp4"

    def test_names_sort_in_sequence_order(self):
        names = [clip_filename("x.mp4", n, 120) for n in range(1, 121)]
        assert names == sorted(names), "zero padding must keep lexical order equal to numeric order"

    def test_unusable_label_is_dropped_rather_than_producing_a_placeholder(self):
        assert clip_filename("x.mp4", 1, 5, "!!!") == "x_clip_01.mp4"

    def test_long_label_is_truncated(self):
        name = clip_filename("x.mp4", 1, 5, "word " * 40)
        assert len(name) < 100

    def test_every_name_in_a_batch_is_unique(self):
        names = {clip_filename("x.mp4", n, 10, "same label") for n in range(1, 11)}
        assert len(names) == 10


class TestZipFilename:
    def test_derives_from_source(self):
        assert zip_filename("Podcast Ep12.mp4") == "podcast-ep12_clips.zip"
