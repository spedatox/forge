"""AnthropicModel — the real Warden reasoning client.

Streams text deltas during a turn and surfaces tool-use blocks once the turn
resolves. Honors the interrupt signal by stopping the stream promptly. There is
deliberately no model-fallback ladder and no token-escalation recovery (§3
rejected list): one model, fail loud.

Prompt caching is applied here and nowhere else. The Warden's shape makes it
mandatory rather than an optimisation: one job runs up to `max_iterations`
laps, every lap re-sends the entire system prompt, the entire tool array and
the entire transcript so far, and the ledger does not even consider compacting
until the prompt approaches 170 K tokens. Uncached, that is the same six-figure
prefix billed at full rate thirty times over. The engine already avoids
rebuilding the tool array when nothing changed, precisely so this cache holds
(see `_refresh_tools`) — this module is the half that was missing.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from forge.model.base import ModelEvent, TextDelta, ToolUseRequest, UsageReport

# Longer TTL on the parts that outlive a turn, shorter on the part that grows
# every turn. Anthropic requires longer-lived breakpoints to render BEFORE
# shorter-lived ones; the request order is [tools, system, messages], so this
# pairing satisfies that by construction.
_PREFIX_TTL = "1h"        # tools + system: rewritten only on a real change
_CONVERSATION_TTL = "5m"  # transcript tail: rewritten incrementally each lap


def _cache_control(ttl: str) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": ttl}


def _cached_system(system: str) -> list[dict[str, Any]] | str:
    """System prompt as a single cached text block.

    Composed once per job (Seam 7) and byte-stable for its whole life, so it is
    the ideal cache prefix — and the largest fixed cost per lap after the tools.
    An empty prompt is passed through untouched: the API rejects an empty block.
    """
    if not system:
        return system
    return [{"type": "text", "text": system,
             "cache_control": _cache_control(_PREFIX_TTL)}]


def _cached_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool array with a breakpoint on the last definition, which caches all of
    them. `_refresh_tools` only swaps this array when the tool NAMES change, so
    in the normal case these bytes are identical for the life of the job."""
    if not tools:
        return tools
    cached = [dict(t) for t in tools]
    cached[-1] = {**cached[-1], "cache_control": _cache_control(_PREFIX_TTL)}
    return cached


def _cached_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transcript with a breakpoint on the final content block.

    The transcript is append-only within a job, so marking the tail means the
    next lap finds everything up to this point already cached and reads it at a
    fraction of input price instead of re-sending it whole. This is the
    breakpoint that actually bounds a long agentic job's cost, because the
    transcript — not the system prompt — is what grows.

    A string `content` is promoted to a block list so the marker has somewhere
    to live; the API treats the two forms identically.
    """
    if not messages:
        return messages
    out = [dict(m) for m in messages]
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str) and content:
        last["content"] = [{"type": "text", "text": content,
                            "cache_control": _cache_control(_CONVERSATION_TTL)}]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if isinstance(blocks[-1], dict):
            blocks[-1] = {**blocks[-1],
                          "cache_control": _cache_control(_CONVERSATION_TTL)}
        last["content"] = blocks
    else:
        return out  # nothing markable — leave the transcript alone
    out[-1] = last
    return out


class AnthropicModel:
    def __init__(self, model_id: str, api_key: str, max_tokens: int = 4096) -> None:
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for AnthropicModel. Set it, or run the "
                "demo which uses the ScriptedModel and needs no key.")
        # Imported lazily so the package imports without the SDK present.
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=api_key)
        self.model_id = model_id
        self.max_tokens = max_tokens

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[ModelEvent]:
        async with self._client.messages.stream(
            model=self.model_id,
            max_tokens=self.max_tokens,
            system=_cached_system(system),
            messages=_cached_messages(messages),
            tools=_cached_tools(tools),
        ) as stream:
            async for event in stream:
                if signal.is_set():
                    break
                if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                    yield TextDelta(event.delta.text)
            if signal.is_set():
                return
            final = await stream.get_final_message()
            for block in final.content:
                if getattr(block, "type", "") == "tool_use":
                    yield ToolUseRequest(id=block.id, name=block.name, input=dict(block.input))
            usage = getattr(final, "usage", None)
            if usage is not None:
                yield UsageReport(
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                )
