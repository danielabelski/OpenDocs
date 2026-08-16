"""Tests for the LLM response cache.

No real provider is ever contacted: a fake provider counts how often the
network layer *would* have been reached, which is the property that matters —
a cache hit must mean an API call not made, and therefore not paid for.
"""

from __future__ import annotations

import json

import pytest

from opendocs.llm.cache import (
    LLMCache,
    llm_cache_dir,
    llm_cache_enabled,
    reset_shared_cache,
    shared_cache,
)
from opendocs.llm.providers import LLMProvider


class CountingProvider(LLMProvider):
    """A provider that records how many calls reached the transport layer."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.text_calls = 0
        self.json_calls = 0
        self.json_payload = '{"entities": ["Alpha"]}'

    def _call(self, system: str, user: str) -> str:
        self.text_calls += 1
        return f"answer#{self.text_calls} to {user}"

    def _call_json(self, system: str, user: str) -> str:
        self.json_calls += 1
        return self.json_payload


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Give every test its own cache and restore the shared one afterwards."""
    monkeypatch.setenv("OPENDOCS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("OPENDOCS_LLM_CACHE", raising=False)
    reset_shared_cache(LLMCache(tmp_path / "cache" / "llm"))
    yield
    reset_shared_cache(None)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_enabled_by_default(self):
        assert llm_cache_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", "False"])
    def test_disabled_by_env(self, monkeypatch, value):
        monkeypatch.setenv("OPENDOCS_LLM_CACHE", value)
        assert llm_cache_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
    def test_other_values_leave_it_enabled(self, monkeypatch, value):
        monkeypatch.setenv("OPENDOCS_LLM_CACHE", value)
        assert llm_cache_enabled() is True

    def test_cache_dir_follows_the_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENDOCS_CACHE_DIR", str(tmp_path / "custom"))
        assert llm_cache_dir() == tmp_path / "custom" / "llm"

    def test_cache_dir_falls_back_to_xdg(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENDOCS_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert llm_cache_dir() == tmp_path / "xdg" / "opendocs" / "llm"


# ---------------------------------------------------------------------------
# Keying
# ---------------------------------------------------------------------------


class TestKeying:
    def base(self, **over):
        args = {
            "provider": "OpenAIProvider",
            "model": "gpt-4o-mini",
            "system": "sys",
            "user": "usr",
            "kind": "text",
            "temperature": 0.2,
            "max_tokens": 100,
        }
        args.update(over)
        return LLMCache.make_key(**args)

    def test_identical_requests_share_a_key(self):
        assert self.base() == self.base()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("provider", "AnthropicProvider"),
            ("model", "gpt-4o"),
            ("system", "different system"),
            ("user", "different user"),
            ("kind", "json"),
            ("temperature", 0.9),
            ("max_tokens", 4000),
        ],
    )
    def test_every_field_changes_the_key(self, field, value):
        """A response generated under different settings must not be replayed."""
        assert self.base() != self.base(**{field: value})

    def test_extra_is_part_of_the_key(self):
        assert self.base() != self.base(extra={"base_url": "http://localhost:11434"})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestStorage:
    @pytest.fixture
    def cache(self, tmp_path):
        return LLMCache(tmp_path / "store")

    def test_miss_then_hit(self, cache):
        assert cache.get("k") is None
        cache.put("k", "the answer")
        assert cache.get("k") == "the answer"
        assert cache.stats.hits == 1 and cache.stats.misses == 1

    def test_disabled_cache_is_inert(self, tmp_path):
        cache = LLMCache(tmp_path / "store", enabled=False)
        assert cache.put("k", "v") is False
        assert cache.get("k") is None
        assert cache.stats.total == 0

    def test_corrupt_entry_is_a_miss(self, cache):
        cache.put("k", "v")
        cache._path("k").write_text("{not json", encoding="utf-8")
        assert cache.get("k") is None

    def test_entry_missing_response_field_is_a_miss(self, cache):
        cache.put("k", "v")
        cache._path("k").write_text(json.dumps({"nope": 1}), encoding="utf-8")
        assert cache.get("k") is None

    def test_no_partial_files_left_behind(self, cache):
        cache.put("k", "v")
        assert list(cache.dir.rglob("*.tmp")) == []

    def test_clear_and_counts(self, cache):
        cache.put("a", "1")
        cache.put("b", "2")
        assert cache.entry_count() == 2
        assert cache.size_bytes() > 0
        assert cache.clear() == 2
        assert cache.entry_count() == 0

    def test_stats_summary(self, cache):
        assert cache.stats.summary() == "no LLM calls"
        cache.put("a", "1")
        cache.get("a")
        cache.get("missing")
        assert cache.stats.summary() == "1/2 LLM responses cached"


# ---------------------------------------------------------------------------
# Provider integration — the property that saves money
# ---------------------------------------------------------------------------


class TestProviderIntegration:
    def test_repeat_text_call_hits_no_api(self):
        p = CountingProvider(model="m")
        first = p.chat("sys", "hello")
        for _ in range(4):
            assert p.chat("sys", "hello") == first
        assert p.text_calls == 1

    def test_repeat_json_call_hits_no_api(self):
        p = CountingProvider(model="m")
        first = p.chat_json("sys", "extract")
        for _ in range(4):
            assert p.chat_json("sys", "extract") == first
        assert p.json_calls == 1

    def test_different_prompt_calls_again(self):
        p = CountingProvider(model="m")
        p.chat("sys", "one")
        p.chat("sys", "two")
        assert p.text_calls == 2

    def test_different_model_calls_again(self):
        a = CountingProvider(model="model-a")
        b = CountingProvider(model="model-b")
        a.chat("sys", "same")
        b.chat("sys", "same")
        assert a.text_calls == 1 and b.text_calls == 1

    def test_text_and_json_do_not_collide(self):
        """Same prompt, different call kind — must not share an entry."""
        p = CountingProvider(model="m")
        p.chat("sys", "same")
        p.chat_json("sys", "same")
        assert p.text_calls == 1 and p.json_calls == 1

    def test_separate_provider_instances_share_the_cache(self):
        """LLMExtractor and LLMSummarizer are different objects, one run."""
        a = CountingProvider(model="m")
        b = CountingProvider(model="m")
        a.chat("sys", "shared question")
        b.chat("sys", "shared question")
        assert a.text_calls == 1
        assert b.text_calls == 0

    def test_cache_survives_a_new_cache_object(self, tmp_path):
        """The on-disk entry is what makes a *later run* free."""
        a = CountingProvider(model="m")
        a.chat("sys", "persisted")

        reset_shared_cache(LLMCache(tmp_path / "cache" / "llm"))
        b = CountingProvider(model="m")
        b.chat("sys", "persisted")
        assert b.text_calls == 0

    def test_disabled_cache_always_calls_the_api(self, tmp_path):
        reset_shared_cache(LLMCache(tmp_path / "cache" / "llm", enabled=False))
        p = CountingProvider(model="m")
        for _ in range(3):
            p.chat("sys", "hello")
        assert p.text_calls == 3

    def test_unparseable_json_is_not_cached(self):
        """Caching a broken response would make the failure permanent."""
        p = CountingProvider(model="m")
        p.json_payload = "not json at all"
        p.max_retries = 1
        with pytest.raises(RuntimeError):
            p.chat_json("sys", "extract")
        assert shared_cache().entry_count() == 0

    def test_json_cache_stores_raw_not_parsed(self):
        """Storing raw text keeps the entry faithful to what the provider said."""
        p = CountingProvider(model="m")
        p.json_payload = '```json\n{"entities": ["Alpha"]}\n```'
        assert p.chat_json("sys", "extract") == {"entities": ["Alpha"]}

        entry = next(shared_cache().dir.rglob("*.json"))
        stored = json.loads(entry.read_text(encoding="utf-8"))["response"]
        assert stored.startswith("```json")

    def test_retries_are_not_defeated_by_the_cache(self):
        """A transient failure then success must still end up cached."""
        attempts = {"n": 0}

        class FlakyProvider(CountingProvider):
            def _call(self, system, user):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("transient")
                return "eventually fine"

        p = FlakyProvider(model="m", max_retries=3)
        assert p.chat("sys", "flaky") == "eventually fine"
        assert p.chat("sys", "flaky") == "eventually fine"
        assert attempts["n"] == 2  # one failure, one success, then cached
