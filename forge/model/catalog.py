"""What models this machine can actually reach, asked of the providers.

The model ref in a profile is a fact about a *deployment* — it is pinned there
so a job runs the same way twice. Choosing a different one mid-session is a
different act, and until now it needed the exact id typed from memory, with a
typo surfacing as a provider error two seconds into the next turn.

So this asks each provider what it serves. Every provider in the fleet answers
the same question (Anthropic under its own SDK, the rest through the
OpenAI-compatible client they already share), which means the list is what the
key can actually reach today — not a table in this file that goes stale the
week a new model ships.

**Only providers with a key configured are asked.** A provider with no key
cannot serve anything, and listing its models would offer a choice that fails
on selection. Ollama is the exception in mechanism and not in principle: it has
no key, so the equivalent question is whether its endpoint answers at all.

Failures are per-provider and are kept, not raised. One dead provider must not
cost the operator the other four — the picker shows the error under that
provider's heading, which is also the fastest way to find out a key is wrong.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("forge.model")

# provider → the env var that turns it on. Ollama is absent deliberately: it
# has no key, and `configured_providers` reaches it through a different test.
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "zai": "ZAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

_SETTINGS_ATTR = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
    "zai": "zai_api_key",
    "deepseek": "deepseek_api_key",
}

# Substrings that mark a model this agent cannot drive: embeddings, speech,
# images, moderation. They are a large fraction of what OpenAI and Gemini
# return, and every one of them fails the same way — a turn that dies on the
# first request with a shape error.
#
# Matched, never guessed at: a name that is merely unfamiliar stays in the
# list. The count of what was hidden travels with the group, so an operator who
# expected to see something knows to look past the filter.
_NON_CHAT = ("embed", "tts", "whisper", "dall-e", "moderation", "imagen",
             "sora", "veo", "transcribe", "audio", "rerank", "image")

_TTL_S = 600.0
"""How long a fetched list stays fresh. Long enough that reopening the picker
is instant, short enough that a key added five minutes ago shows up."""

_CACHE: dict[str, tuple[float, "ProviderCatalog"]] = {}


@dataclass(frozen=True)
class ModelInfo:
    """One model an operator can pick."""

    provider: str
    model_id: str
    created: int | None = None
    label: str = ""             # display name, when the provider offers one

    @property
    def ref(self) -> str:
        """The `provider:model` string the factory takes.

        Always qualified, including for Anthropic: a bare name works, but the
        ref is also what `/model` echoes back and what the session persists,
        and a qualified one says where it runs without the reader having to
        know the routing table.
        """
        return f"{self.provider}:{self.model_id}"


@dataclass(frozen=True)
class ProviderCatalog:
    """What one provider answered. A non-empty `error` means it did not."""

    provider: str
    models: tuple[ModelInfo, ...] = ()
    hidden: int = 0             # non-chat entries filtered out of `models`
    error: str = ""


def configured_providers(settings: Any) -> list[str]:
    """Providers this machine has credentials for, in a stable order.

    Ollama is included whenever a base URL is set, which it is by default. That
    is not a claim it is running — a local daemon that is switched off answers
    the fetch with a connection error, and that error is worth showing under
    its own heading rather than hiding the provider entirely.
    """
    out = [p for p, attr in _SETTINGS_ATTR.items() if getattr(settings, attr, "")]
    if getattr(settings, "ollama_base_url", ""):
        out.append("ollama")
    return out


def _short_error(exc: BaseException) -> str:
    """One line an operator can act on.

    Provider SDKs raise with paragraphs — a JSON body, a request id, a docs
    link. The first line names the cause (401, connection refused) and the rest
    is furniture that would push the other providers off the screen.
    """
    lines = str(exc).strip().splitlines()
    head = lines[0] if lines else ""
    if len(head) > 140:
        head = head[:137] + "…"
    return f"{exc.__class__.__name__}: {head}" if head else exc.__class__.__name__


def _sort_key(model: ModelInfo) -> tuple[int, int, str]:
    """Newest first where the provider dates its models, then by id.

    Sorting by name alone puts `claude-3-haiku` above `claude-sonnet-4-6`,
    which is the opposite of what someone scanning the list wants. Providers
    that send no timestamp keep a stable alphabetical order rather than
    whatever order the response happened to arrive in.
    """
    return (0 if model.created else 1, -(model.created or 0), model.model_id)


def _classify(provider: str, rows: list[tuple[str, int | None, str]]) -> ProviderCatalog:
    kept: list[ModelInfo] = []
    hidden = 0
    for model_id, created, label in rows:
        if any(bad in model_id.lower() for bad in _NON_CHAT):
            hidden += 1
            continue
        kept.append(ModelInfo(provider=provider, model_id=model_id,
                              created=created, label=label))
    kept.sort(key=_sort_key)
    return ProviderCatalog(provider=provider, models=tuple(kept), hidden=hidden)


async def _fetch_anthropic(settings: Any, timeout: float) -> ProviderCatalog:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=timeout)
    try:
        page = await client.models.list(limit=100)
    finally:
        await client.close()
    rows: list[tuple[str, int | None, str]] = []
    for m in getattr(page, "data", None) or []:
        created = getattr(m, "created_at", None)
        # The Anthropic SDK parses this into a datetime; the compat endpoints
        # send an epoch int. There is one sort key, so normalise here.
        stamp = int(created.timestamp()) if hasattr(created, "timestamp") else None
        rows.append((str(m.id), stamp, str(getattr(m, "display_name", "") or "")))
    return _classify("anthropic", rows)


async def _fetch_openai_compat(provider: str, settings: Any,
                               timeout: float) -> ProviderCatalog:
    from openai import AsyncOpenAI

    from forge.model.providers import _OPENAI_COMPAT

    kwargs = _OPENAI_COMPAT[provider](settings)
    client = AsyncOpenAI(timeout=timeout, max_retries=0, **kwargs)
    try:
        page = await client.models.list()
    finally:
        await client.close()
    rows: list[tuple[str, int | None, str]] = []
    for m in getattr(page, "data", None) or []:
        model_id = str(getattr(m, "id", "") or "")
        # Gemini's OpenAI-compat layer returns fully-qualified resource names
        # ("models/gemini-2.5-pro"). The chat endpoint accepts either, but the
        # prefix is noise in a list and would travel into the saved ref.
        if model_id.startswith("models/"):
            model_id = model_id[len("models/"):]
        if not model_id:
            continue
        created = getattr(m, "created", None)
        rows.append((model_id,
                     int(created) if isinstance(created, (int, float)) else None,
                     str(getattr(m, "display_name", "") or "")))
    return _classify(provider, rows)


async def fetch(provider: str, settings: Any, *, timeout: float = 8.0) -> ProviderCatalog:
    """Ask one provider what it serves. Never raises — see the module docstring."""
    try:
        if provider == "anthropic":
            coro = _fetch_anthropic(settings, timeout)
        else:
            coro = _fetch_openai_compat(provider, settings, timeout)
        # The SDK timeout governs each HTTP attempt; this one governs the whole
        # call, so a provider that stalls between retries still lets go of the
        # picker.
        return await asyncio.wait_for(coro, timeout + 2)
    except asyncio.TimeoutError:
        return ProviderCatalog(provider=provider, error=f"no answer within {timeout:.0f}s")
    except ImportError as e:
        # The `providers` extra is not installed. Name the install that fixes
        # it, because "No module named 'openai'" under a provider heading does
        # not tell an operator what to do about it.
        return ProviderCatalog(provider=provider,
                               error=f'{e} — pip install -e ".[providers]"')
    except Exception as e:  # noqa: BLE001 — one provider failing is data, not a crash
        # Debug, not warning: the operator is already being shown this error
        # under the provider's own heading, and a log line written while the
        # picker is repainting lands in the middle of its frame. Kept at all
        # because `FORGE_LOG_LEVEL=DEBUG` is where the full body goes when the
        # one-line version is not enough.
        logger.debug("model_list_failed", extra={"provider": provider, "error": str(e)})
        return ProviderCatalog(provider=provider, error=_short_error(e))


async def catalog(settings: Any, *, refresh: bool = False,
                  timeout: float = 8.0) -> list[ProviderCatalog]:
    """Every configured provider's models, fetched concurrently.

    Concurrent because the slow case is five providers in series, each waiting
    on a network round trip, in front of an operator who just pressed a key.
    Cached because the second thing an operator does after picking a model is
    pick a different one.
    """
    providers = configured_providers(settings)
    if refresh:
        for provider in providers:
            _CACHE.pop(provider, None)

    now = time.monotonic()
    have: dict[str, ProviderCatalog] = {}
    for provider in providers:
        at, cached = _CACHE.get(provider, (0.0, None))
        if cached is not None and now - at < _TTL_S:
            have[provider] = cached

    missing = [p for p in providers if p not in have]
    if missing:
        for result in await asyncio.gather(
                *(fetch(p, settings, timeout=timeout) for p in missing)):
            # Errors are cached too, briefly: without that, a picker reopened
            # against an unreachable provider pays the full timeout again every
            # single time — the case where waiting is least tolerable.
            _CACHE[result.provider] = (now, result)
            have[result.provider] = result
    return [have[p] for p in providers]


def invalidate() -> None:
    """Drop the cache, for `/model refresh` after a key changes."""
    _CACHE.clear()
