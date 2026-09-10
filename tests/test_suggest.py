"""Tests for the suggestion layer.

The point is not that the model gives good answers; that is a judgement call
on real content. It is that a *bad* answer cannot become a wrong cut, and
that the local-model provider sends exactly what Ollama needs.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app import suggest
from app.suggest import (
    DisabledProvider,
    OllamaProvider,
    SuggestedClip,
    SuggestionError,
    SuggestionResponse,
    estimate_tokens,
    format_transcript,
    get_provider,
    resolve,
    set_provider,
    suggest_clips,
    window_segments,
)
from app.transcription import TranscriptSegment
from tests.conftest import FakeSuggestions


def segments(count: int = 5, length: float = 10.0) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(index=i, start=i * length, end=(i + 1) * length, text=f"sentence number {i}")
        for i in range(count)
    ]


class TestResolveRejectsHallucinations:
    def test_timestamps_come_from_the_transcript_not_the_model(self):
        segs = segments()
        (result,) = resolve([SuggestedClip(start_segment=1, end_segment=2, title="A", reason="r")], segs)
        assert result.start == segs[1].start == 10.0
        assert result.end == segs[2].end == 30.0

    def test_segment_index_that_does_not_exist_is_dropped(self):
        assert resolve(
            [SuggestedClip(start_segment=999, end_segment=1000, title="Ghost", reason="r")], segments()
        ) == []

    def test_negative_index_is_dropped(self):
        assert resolve([SuggestedClip(start_segment=-1, end_segment=2, title="Bad", reason="r")], segments()) == []

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
        assert resolve([SuggestedClip(start_segment=4, end_segment=0, title="Rev", reason="r")], segments()) == []

    def test_single_segment_clip_is_valid(self):
        (result,) = resolve([SuggestedClip(start_segment=2, end_segment=2, title="One", reason="r")], segments())
        assert (result.start, result.end) == (20.0, 30.0)

    def test_empty_title_gets_a_placeholder_not_an_empty_filename(self):
        (result,) = resolve([SuggestedClip(start_segment=0, end_segment=1, title="  ", reason="r")], segments())
        assert result.title == "Untitled clip"

    def test_empty_inputs(self):
        assert resolve([], segments()) == []
        assert resolve([SuggestedClip(start_segment=0, end_segment=1, title="X", reason="r")], []) == []


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
        results = resolve(
            [
                SuggestedClip(start_segment=0, end_segment=3, title="Wide", reason="r"),
                SuggestedClip(start_segment=2, end_segment=4, title="Overlap", reason="r"),
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
        assert "[0]" in rendered and "[2]" in rendered and "sentence number 1" in rendered

    def test_each_line_carries_a_readable_timestamp(self):
        assert "(00:00:10)" in format_transcript(segments(3))

    def test_schema_forbids_the_model_returning_raw_seconds(self):
        fields = set(SuggestedClip.model_fields)
        assert fields == {"start_segment", "end_segment", "title", "reason"}
        assert not any("second" in f for f in fields)

    def test_schema_coerces_numeric_strings_from_small_models(self):
        # 7B models sometimes emit "12" rather than 12; that must not be fatal.
        r = SuggestionResponse.model_validate({"clips": [{"start_segment": "1", "end_segment": "2", "title": "t", "reason": "r"}]})
        assert r.clips[0].start_segment == 1

    def test_schema_rejects_a_response_missing_required_fields(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SuggestionResponse.model_validate({"clips": [{"start_segment": 0}]})


class TestWindowing:
    def test_short_transcript_is_one_window(self):
        assert len(window_segments(segments(5), max_tokens=10_000)) == 1

    def test_long_transcript_is_split_on_segment_boundaries(self):
        segs = segments(40)
        per_line = estimate_tokens("[10] (00:00:00) sentence number 10\n")
        windows = window_segments(segs, max_tokens=per_line * 10)
        assert len(windows) >= 4
        assert sum(len(w) for w in windows) == 40
        flat = [s.index for w in windows for s in w]
        assert flat == list(range(40)), "every segment appears exactly once, in order"

    def test_a_single_oversized_segment_still_gets_a_window(self):
        big = [TranscriptSegment(0, 0, 10, "x" * 5000)]
        assert len(window_segments(big, max_tokens=10)) == 1

    def test_suggest_clips_spreads_the_target_across_windows(self):
        provider = FakeSuggestions(clips=[])
        provider.max_input_tokens = estimate_tokens("[10] (00:00:00) sentence number 10\n") * 10
        suggest_clips(segments(40), target_count=8, provider=provider)
        assert provider.calls >= 4, "a long transcript must be sent in several windows"


class TestOllamaProvider:
    def _provider(self, monkeypatch, model="qwen2.5:7b-instruct", num_ctx=16384):
        monkeypatch.setattr(
            suggest, "settings",
            SimpleNamespace(ollama_url="http://ollama:11434", suggest_model=model,
                            ollama_num_ctx=num_ctx, suggest_timeout_seconds=30),
        )
        return OllamaProvider()

    def test_sends_a_schema_constrained_chat_request(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured.update(url=url, body=json, timeout=timeout)
            return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"clips": []}'}})

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = self._provider(monkeypatch)
        result = provider.propose("[0] (00:00:00) hi", 3, 1)

        assert result.clips == []
        assert captured["url"] == "http://ollama:11434/api/chat"
        body = captured["body"]
        assert body["model"] == "qwen2.5:7b-instruct"
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0
        assert body["options"]["num_ctx"] == 16384
        assert body["format"]["properties"]["clips"], "format must carry the JSON schema"
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert "segment numbers" in body["messages"][0]["content"].lower() or "segment" in body["messages"][0]["content"]

    def test_parses_the_models_json_answer(self, monkeypatch):
        answer = {"clips": [{"start_segment": 1, "end_segment": 2, "title": "T", "reason": "R"}]}
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            200, json={"message": {"content": json.dumps(answer)}}))
        result = self._provider(monkeypatch).propose("x", 1, 3)
        assert result.clips[0].start_segment == 1

    def test_unreachable_ollama_is_a_clear_error_not_a_crash(self, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(httpx, "post", boom)
        with pytest.raises(SuggestionError, match="could not reach Ollama"):
            self._provider(monkeypatch).propose("x", 1, 1)

    def test_http_error_is_reported(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(404, text="model not found"))
        with pytest.raises(SuggestionError, match="404"):
            self._provider(monkeypatch).propose("x", 1, 1)

    def test_answer_that_breaks_the_schema_is_rejected(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            200, json={"message": {"content": '{"clips": [{"start_seconds": 847}]}'}}))
        with pytest.raises(SuggestionError, match="schema"):
            self._provider(monkeypatch).propose("x", 1, 1)

    def test_window_size_leaves_room_for_prompt_and_answer(self, monkeypatch):
        provider = self._provider(monkeypatch, num_ctx=16384)
        assert provider.max_input_tokens < 16384
        assert provider.max_input_tokens >= 10000


class TestProviderSelection:
    def test_suite_default_is_disabled(self):
        set_provider(None)
        provider = get_provider()
        set_provider(None)
        assert isinstance(provider, DisabledProvider)

    def test_disabled_provider_raises_not_returns_empty(self):
        with pytest.raises(SuggestionError, match="disabled"):
            DisabledProvider().propose("x", 1, 1)

    def test_suggest_clips_uses_the_injected_provider(self):
        fake = FakeSuggestions()
        results = suggest_clips(segments(6), target_count=2, provider=fake)
        assert fake.calls == 1
        assert [r.title for r in results] == ["Founder origin story", "Closing line"]

    def test_empty_transcript_never_calls_the_model(self):
        fake = FakeSuggestions()
        assert suggest_clips([], provider=fake) == []
        assert fake.calls == 0
