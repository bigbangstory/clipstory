"""Tests for the transcription layer.

faster-whisper is not required: the interface, the disabled short-circuit and
the ffmpeg audio extraction are what is under test.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import transcription
from app.media import extract_audio, probe
from app.transcription import (
    NullTranscriptionProvider,
    TranscriptionError,
    TranscriptSegment,
    Word,
    get_provider,
    set_provider,
)
from tests.conftest import FakeTranscription, requires_ffmpeg


class TestSegmentModel:
    def test_duration_is_derived(self):
        assert TranscriptSegment(index=0, start=1.0, end=4.5, text="hi").duration == 3.5

    def test_words_default_to_empty_rather_than_none(self):
        assert TranscriptSegment(index=0, start=0, end=1, text="hi").words == []

    def test_words_carry_their_own_timings(self):
        segment = TranscriptSegment(
            index=0, start=0, end=1, text="hello there",
            words=[Word(0.0, 0.4, "hello"), Word(0.4, 1.0, "there")],
        )
        assert segment.words[1].text == "there"
        assert segment.words[1].start == 0.4


class TestProviderSelection:
    def test_provider_is_swappable(self):
        fake = FakeTranscription()
        set_provider(fake)
        try:
            assert get_provider() is fake
        finally:
            set_provider(None)

    def test_suite_default_is_the_disabled_provider(self):
        # run-tests.sh sets TRANSCRIPTION_PROVIDER=disabled so no test can
        # accidentally download Whisper weights.
        set_provider(None)
        provider = get_provider()
        set_provider(None)
        assert isinstance(provider, NullTranscriptionProvider)

    def test_disabled_provider_is_flagged_so_the_pipeline_skips_audio_extraction(self):
        assert NullTranscriptionProvider.enabled is False
        assert FakeTranscription.enabled is True

    def test_null_provider_refuses_clearly(self):
        with pytest.raises(TranscriptionError, match="disabled"):
            NullTranscriptionProvider().transcribe(Path("anything.wav"))

    def test_unknown_provider_name_is_rejected_with_the_valid_options(self, monkeypatch):
        set_provider(None)
        monkeypatch.setattr(
            transcription, "settings", SimpleNamespace(transcription_provider="nope")
        )
        try:
            with pytest.raises(TranscriptionError, match="unknown transcription provider"):
                get_provider()
        finally:
            set_provider(None)


@requires_ffmpeg
class TestAudioExtraction:
    def test_produces_16k_mono_pcm(self, marked_source, tmp_path):
        import subprocess

        audio = extract_audio(marked_source, tmp_path / "audio.wav")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "default=nw=1", str(audio)],
            check=True, capture_output=True, text=True,
        )
        assert "codec_name=pcm_s16le" in result.stdout
        assert "sample_rate=16000" in result.stdout
        assert "channels=1" in result.stdout, "Whisper expects mono"

    def test_output_has_no_video_stream(self, marked_source, tmp_path):
        from app.media import MediaError

        audio = extract_audio(marked_source, tmp_path / "audio.wav")
        with pytest.raises(MediaError, match="no video stream"):
            probe(audio)

    def test_size_is_proportional_to_duration(self, marked_source, tmp_path):
        # 16 kHz mono 16-bit is 32 KB/s, so a 30s source is about 960 KB.
        audio = extract_audio(marked_source, tmp_path / "audio.wav")
        expected = 30 * 32000
        assert expected * 0.8 < audio.stat().st_size < expected * 1.2

    def test_missing_source_raises(self, tmp_path):
        from app.media import MediaError

        with pytest.raises(MediaError):
            extract_audio(tmp_path / "nope.mp4", tmp_path / "out.wav")
