"""Listing what each provider serves.

The point of asking the endpoint rather than keeping a table is that the answer
stays true; the point of asking every configured provider *concurrently*, and
keeping failures instead of raising them, is that one dead key must not cost
the operator the other four. Both are load-bearing enough to pin down here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from forge.model import catalog


@dataclass
class _Settings:
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    zai_api_key: str = ""
    deepseek_api_key: str = ""
    ollama_base_url: str = ""


@pytest.fixture(autouse=True)
def _clean_cache():
    catalog.invalidate()
    yield
    catalog.invalidate()


def test_only_providers_with_a_key_are_asked():
    settings = _Settings(openai_api_key="sk-x", zai_api_key="z")
    assert catalog.configured_providers(settings) == ["openai", "zai"]


def test_ollama_counts_as_configured_by_its_url_since_it_has_no_key():
    assert catalog.configured_providers(_Settings(ollama_base_url="http://x/v1")) == ["ollama"]
    assert catalog.configured_providers(_Settings()) == []


def test_non_chat_models_are_filtered_and_counted():
    result = catalog._classify("openai", [
        ("gpt-5.1", 300, ""),
        ("text-embedding-3-large", 200, ""),
        ("whisper-1", 100, ""),
    ])
    assert [m.model_id for m in result.models] == ["gpt-5.1"]
    assert result.hidden == 2


def test_newest_first_then_undated_alphabetically():
    result = catalog._classify("openai", [
        ("older", 100, ""), ("zeta", None, ""), ("newer", 900, ""), ("alpha", None, ""),
    ])
    assert [m.model_id for m in result.models] == ["newer", "older", "alpha", "zeta"]


def test_a_ref_is_always_provider_qualified():
    assert catalog.ModelInfo(provider="anthropic", model_id="claude-x").ref \
        == "anthropic:claude-x"


def test_one_providers_failure_is_kept_not_raised(monkeypatch):
    async def _fetch(provider, settings, timeout):
        if provider == "openai":
            raise RuntimeError("401 invalid_api_key\nrequest id: abc\nsee docs")
        return catalog.ProviderCatalog(provider=provider,
                                       models=(catalog.ModelInfo(provider, "m"),))

    monkeypatch.setattr(catalog, "_fetch_openai_compat", _fetch)
    settings = _Settings(openai_api_key="bad", zai_api_key="good")
    groups = asyncio.run(catalog.catalog(settings, timeout=1.0))

    by_provider = {g.provider: g for g in groups}
    assert by_provider["openai"].error.startswith("RuntimeError: 401 invalid_api_key")
    assert "request id" not in by_provider["openai"].error   # one line, not the body
    assert by_provider["zai"].models                          # the good one survived


def test_a_stalled_provider_gives_up_instead_of_holding_the_picker(monkeypatch):
    async def _hang(provider, settings, timeout):
        await asyncio.sleep(30)

    monkeypatch.setattr(catalog, "_fetch_openai_compat", _hang)

    async def _go():
        return await catalog.fetch("openai", _Settings(openai_api_key="k"), timeout=0.01)

    result = asyncio.run(_go())
    assert "no answer" in result.error


def test_providers_are_fetched_concurrently(monkeypatch):
    """Five providers in series, each a network round trip, in front of someone
    who just pressed a key — the whole reason `catalog` gathers."""
    started = 0

    async def _slow(provider, settings, timeout):
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)
        return catalog.ProviderCatalog(provider=provider,
                                       models=(catalog.ModelInfo(provider, "m"),))

    monkeypatch.setattr(catalog, "_fetch_openai_compat", _slow)
    settings = _Settings(openai_api_key="a", zai_api_key="b", deepseek_api_key="c")

    async def _go():
        loop = asyncio.get_running_loop()
        at = loop.time()
        groups = await catalog.catalog(settings)
        return groups, loop.time() - at

    groups, elapsed = asyncio.run(_go())
    assert started == 3
    assert len(groups) == 3
    assert elapsed < 0.14          # serial would be 0.15


def test_the_second_look_is_served_from_cache(monkeypatch):
    calls = 0

    async def _count(provider, settings, timeout):
        nonlocal calls
        calls += 1
        return catalog.ProviderCatalog(provider=provider,
                                       models=(catalog.ModelInfo(provider, "m"),))

    monkeypatch.setattr(catalog, "_fetch_openai_compat", _count)
    settings = _Settings(openai_api_key="k")

    asyncio.run(catalog.catalog(settings))
    asyncio.run(catalog.catalog(settings))
    assert calls == 1

    asyncio.run(catalog.catalog(settings, refresh=True))
    assert calls == 2


def test_a_failure_is_cached_too_so_a_dead_provider_is_not_waited_on_twice(monkeypatch):
    calls = 0

    async def _fail(provider, settings, timeout):
        nonlocal calls
        calls += 1
        raise RuntimeError("connection refused")

    monkeypatch.setattr(catalog, "_fetch_openai_compat", _fail)
    settings = _Settings(ollama_base_url="http://127.0.0.1:1/v1")

    first = asyncio.run(catalog.catalog(settings, timeout=0.5))
    second = asyncio.run(catalog.catalog(settings, timeout=0.5))
    assert calls == 1
    assert first[0].error == second[0].error


def test_gemini_resource_names_are_stripped_to_the_id(monkeypatch):
    class _Model:
        def __init__(self, mid):
            self.id, self.created = mid, 100

    class _Page:
        data = [_Model("models/gemini-2.5-pro"), _Model("gemini-2.5-flash")]

    class _Client:
        def __init__(self, **kwargs):
            self.models = self
            self.closed = False

        async def list(self):
            return _Page()

        async def close(self):
            self.closed = True

    import sys
    import types

    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake)

    result = asyncio.run(catalog._fetch_openai_compat(
        "gemini", _Settings(gemini_api_key="k"), 5.0))
    assert [m.model_id for m in result.models] == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert [m.ref for m in result.models][0] == "gemini:gemini-2.5-flash"


def test_a_missing_providers_extra_names_the_install_that_fixes_it(monkeypatch):
    async def _no_module(provider, settings, timeout):
        raise ImportError("No module named 'openai'")

    monkeypatch.setattr(catalog, "_fetch_openai_compat", _no_module)
    result = asyncio.run(catalog.fetch("openai", _Settings(openai_api_key="k")))
    assert "pip install" in result.error
