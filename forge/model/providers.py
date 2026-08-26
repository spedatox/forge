"""Multi-provider model routing, mirroring S.P.E.D.A. Mark VI's llm_client.

Routing is driven by the model ref: ``"provider:model"`` — e.g.
``openai:gpt-5.1``, ``gemini:gemini-2.5-flash``, ``zai:glm-4.6``,
``deepseek:deepseek-v4-pro``, ``ollama:llama3.1:8b``. A bare name routes to
Anthropic, so every existing profile keeps working.

The Warden loop speaks Anthropic content-block format throughout (its message
history is text / tool_use / tool_result blocks). Anthropic calls pass straight
through the native SDK (`AnthropicModel`). Every other provider shares ONE adapter
(`OpenAICompatModel`) built on OpenAI's client — OpenAI, Gemini's OpenAI-compat
endpoint, z.ai's paas/v4, DeepSeek's endpoint, and Ollama's /v1 all speak the same
chat-completions dialect. Translation to and from that dialect happens only at the
request boundary; the loop and tools never change.

This mirrors Mark VI's engine so an operator sees consistent behavior whether a
turn runs in-process on Mark VI or out here on the Forge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

from forge.model.base import ModelEvent, TextDelta, ToolUseRequest, TurnEnd, UsageReport


def _usage_report(usage: Any) -> UsageReport | None:
    """Normalize a provider's usage object, whatever it calls its fields.

    Yielding nothing is a legitimate outcome: usage is optional by contract and
    the ledger estimates when a provider stays silent. Most OpenAI-compatible
    endpoints only send usage when asked via `stream_options`, which is not
    universally supported across the providers this adapter serves — so this
    reads what arrives rather than demanding it."""
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "input_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", None)
    if completion is None:
        completion = getattr(usage, "output_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
    # Providers report prompt_tokens INCLUSIVE of cached tokens, the opposite of
    # Anthropic. Split them so the ledger's arithmetic means the same thing on
    # both sides.
    return UsageReport(input_tokens=max(0, prompt - cached),
                       output_tokens=completion, cache_read=cached)

logger = logging.getLogger("forge.model")

# Provider → AsyncOpenAI constructor kwargs (given the settings object). Lazy so
# a missing key only matters when that provider is actually selected.
_OPENAI_COMPAT = {
    "openai": lambda s: {"api_key": s.openai_api_key},
    "gemini": lambda s: {"api_key": s.gemini_api_key,
                         "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    "zai": lambda s: {"api_key": s.zai_api_key,
                      "base_url": "https://api.z.ai/api/paas/v4/"},
    "deepseek": lambda s: {"api_key": s.deepseek_api_key,
                           "base_url": "https://api.deepseek.com"},
    "ollama": lambda s: {"api_key": "ollama", "base_url": s.ollama_base_url},
}
_PROVIDERS = {"anthropic", *_OPENAI_COMPAT}

# chat-completions finish_reason → the vocabulary TurnEnd speaks, which is
# Anthropic's. The loop's stop signal is still whether any ToolUseRequest was
# yielded; what this adds is the one case that signal cannot see — "length",
# meaning the turn was cut off at the cap and its text is half of something.
_FINISH_TO_STOP = {"stop": "end_turn", "tool_calls": "tool_use",
                   "function_call": "tool_use", "length": "max_tokens"}


def _turn_end(finish: str | None, report: UsageReport | None, cap: int) -> TurnEnd:
    """What the provider said about why it stopped, plus the fallback inference.

    Providers in this family are inconsistent about both halves: some send no
    usage unless asked via `stream_options`, some send a finish_reason of None
    on the last chunk. Reading whichever arrived beats requiring either."""
    return TurnEnd(
        reason=_FINISH_TO_STOP.get(finish or "", finish or None),
        truncated_estimate=bool(report and cap > 0 and report.output_tokens >= cap),
    )


# OpenAI reasoning models whose function tools must go to /v1/responses rather
# than /v1/chat/completions. Named releases first, then a version-number match so
# future ones (5.7, 6.x, …) are covered without another edit here.
_GPT56_CODENAMES = ("terra", "luna", "sol")


def _openai_tools_need_responses_api(model_l: str) -> bool:
    """True for gpt-5.6-and-newer OpenAI models, whose tools need /v1/responses.

    During the 5.6 preview, chat-completions accepted tools as long as
    reasoning_effort was forced to 'none'. After the July 9 2026 GA that stopped
    being reliable: the same key/model/payload fails roughly every other call
    with a misleading 401 "insufficient permissions" (luna) or a clean 400
    (sol/terra), then succeeds on retry. OpenAI's own error text names the fix —
    "To use function tools, use /v1/responses" — so we route there instead of
    reintroducing the reasoning_effort hack. Mark VI reached the same conclusion
    the same way; this mirrors its llm_client so a turn behaves identically
    whether it runs in-process there or out here.

    The older gpt-5 generation (gpt-5 / mini / nano) is unaffected and stays on
    chat-completions.
    """
    import re

    if any(c in model_l for c in _GPT56_CODENAMES):
        return True
    m = re.search(r"gpt-(\d+(?:\.\d+)?)", model_l)
    if m:
        try:
            return float(m.group(1)) >= 5.6
        except ValueError:
            return False
    return False


def _use_responses_api(provider: str, model: str, tools: list[dict[str, Any]] | None) -> bool:
    """Whether this specific call must use /v1/responses. Only ever true for
    OpenAI + a 5.6-family model + tools in the request. Tool-free calls work
    fine on chat-completions and stay there, so nothing else moves."""
    return bool(tools) and provider == "openai" and _openai_tools_need_responses_api(model.lower())


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split ``provider:model`` → (provider, model). Bare names are Anthropic.
    Only the first segment is matched, so Ollama tags like ``llama3.1:8b`` survive
    inside ``ollama:llama3.1:8b``."""
    provider, sep, rest = ref.partition(":")
    if sep and provider in _PROVIDERS:
        return provider, rest
    return "anthropic", ref


class OpenAICompatModel:
    """One adapter for every OpenAI-compatible provider. Implements the Warden's
    `Model` protocol: streams TextDelta during the turn, then yields a
    ToolUseRequest per accumulated tool call."""

    def __init__(self, provider: str, model_id: str, settings: Any, max_tokens: int = 4096) -> None:
        if provider not in _OPENAI_COMPAT:
            raise ValueError(f"not an OpenAI-compatible provider: {provider!r}")
        kwargs = _OPENAI_COMPAT[provider](settings)
        if provider != "ollama" and not kwargs.get("api_key"):
            raise RuntimeError(
                f"{provider} model requested but its API key is not set "
                f"(expected {provider.upper()}_API_KEY).")
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(**kwargs)
        self.provider = provider
        self.model_id = model_id
        self.max_tokens = max_tokens

    async def close(self) -> None:
        """Release the SDK client's connection pool. Without this the client
        (and the httpx pool underneath it) is reclaimed only by GC, which for an
        async client means a teardown that runs after the loop has moved on —
        the "generator didn't stop after athrow()" noise a job otherwise leaves
        in the logs on every run."""
        await self._client.close()

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[TextDelta | ToolUseRequest]:
        if _use_responses_api(self.provider, self.model_id, tools):
            async for item in self._stream_responses(system, messages, tools, signal):
                yield item
            return

        params = _to_openai_params(self.provider, self.model_id, system, messages,
                                   tools, self.max_tokens)
        raw = await self._client.chat.completions.create(**params, stream=True)
        acc: dict[int, dict[str, str]] = {}
        usage: Any = None
        finish: str | None = None
        reasoning_parts: list[str] = []
        try:
            async for chunk in raw:
                if signal.is_set():
                    break
                usage = getattr(chunk, "usage", None) or usage
                if not chunk.choices:
                    continue
                finish = getattr(chunk.choices[0], "finish_reason", None) or finish
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                # DeepSeek's reasoning models stream their chain-of-thought as
                # `reasoning_content` in `model_extra` (the OpenAI SDK has no typed
                # field for it). Captured, not shown: it must travel back WITH the
                # assistant message that carries the tool call, or DeepSeek rejects
                # the replay once thinking is enabled.
                if getattr(delta, "reasoning_content", None):
                    reasoning_parts.append(delta.reasoning_content)
                if delta.content:
                    yield TextDelta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        finally:
            await raw.close()
        if signal.is_set():
            return
        reasoning = "".join(reasoning_parts) or None
        for idx in sorted(acc):
            slot = acc[idx]
            yield ToolUseRequest(id=slot["id"] or _gen_tool_id(),
                                 name=slot["name"],
                                 input=_parse_tool_args(slot["arguments"], slot["name"]),
                                 reasoning_content=reasoning)
        report = _usage_report(usage)
        if report is not None:
            yield report
        yield _turn_end(finish, report, self.max_tokens)

    async def _stream_responses(self, system: str, messages: list[dict[str, Any]],
                                tools: list[dict[str, Any]], signal: asyncio.Event
                                ) -> AsyncIterator[ModelEvent]:
        """The /v1/responses path, yielding exactly what the caller above does.

        The event shape is different from chat-completions: text arrives as
        `response.output_text.delta`, and a function call arrives as an
        `output_item.added` item carrying its call_id and name, followed by its
        arguments JSON in deltas keyed by the same output_index.
        """
        params = _to_responses_params(self.model_id, system, messages, tools, self.max_tokens)
        raw = await self._client.responses.create(**params, stream=True)
        # output_index → accumulating call.
        calls: dict[int, dict[str, str]] = {}
        usage: Any = None
        finish: str | None = None
        try:
            async for event in raw:
                if signal.is_set():
                    break
                etype = getattr(event, "type", "")
                if etype in ("response.completed", "response.incomplete"):
                    response = getattr(event, "response", None)
                    usage = getattr(response, "usage", None) or usage
                    # The Responses API does not use finish_reason. A turn cut
                    # off at the cap arrives as response.incomplete carrying
                    # incomplete_details.reason == "max_output_tokens".
                    details = getattr(response, "incomplete_details", None)
                    if getattr(details, "reason", None) == "max_output_tokens":
                        finish = "length"
                if etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        yield TextDelta(delta)
                elif etype == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", None) == "function_call":
                        calls[getattr(event, "output_index", len(calls))] = {
                            # call_id is the handle the API pairs
                            # function_call_output against — item.id is a
                            # different, unusable identifier.
                            "id": getattr(item, "call_id", "") or "",
                            "name": getattr(item, "name", "") or "",
                            "arguments": getattr(item, "arguments", "") or "",
                        }
                elif etype == "response.function_call_arguments.delta":
                    slot = calls.setdefault(getattr(event, "output_index", 0),
                                            {"id": "", "name": "", "arguments": ""})
                    slot["arguments"] += getattr(event, "delta", "") or ""
        finally:
            await raw.close()
        if signal.is_set():
            return
        for idx in sorted(calls):
            slot = calls[idx]
            yield ToolUseRequest(id=slot["id"] or _gen_tool_id(),
                                 name=slot["name"],
                                 input=_parse_tool_args(slot["arguments"], slot["name"]))
        report = _usage_report(usage)
        if report is not None:
            yield report
        yield _turn_end(finish, report, self.max_tokens)


# ── Anthropic content-block → chat-completions translation ───────────────────

def _to_openai_params(provider: str, model: str, system: str,
                      messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                      max_tokens: int) -> dict[str, Any]:
    msgs: list[dict[str, Any]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages:
        msgs.extend(_translate_message(m))

    params: dict[str, Any] = {"model": model, "messages": msgs}

    if tools:
        params["tools"] = [{
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""),
                         "parameters": t.get("input_schema", {"type": "object"})},
        } for t in tools]

    if max_tokens:
        # OpenAI deprecated max_tokens for reasoning models; the others still take it.
        params["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens

    # DeepSeek-V4 defaults thinking ON, but that default is not honoured through
    # the OpenAI-compat endpoint, so ask for it explicitly whenever tools are
    # present — i.e. the whole agent loop. Mirrors Mark VI's llm_client.
    #
    # NOTE: an earlier revision forced {"type": "disabled"} here because thinking
    # made `reasoning_content` a required round-trip field once tool_use entered
    # history, which broke the tool loop. If reasoning_content starts failing to
    # round-trip again, the loop here is where to look first.
    if provider == "deepseek" and tools:
        params["extra_body"] = {"thinking": {"type": "enabled"}}
    return params


def _to_responses_params(model: str, system: str, messages: list[dict[str, Any]],
                         tools: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    """Request body for /v1/responses.

    Differs from chat-completions in every structural way that matters: history
    is a flat `input` item list rather than role messages, tool calls are
    top-level `function_call` items paired with `function_call_output` by
    `call_id` (not nested in messages), tool declarations are flat (no
    {"type":"function","function":{…}} envelope), the system prompt rides
    `instructions`, and the budget parameter is `max_output_tokens`.
    """
    items: list[dict[str, Any]] = []
    for m in messages:
        items.extend(_translate_message_responses(m))

    params: dict[str, Any] = {"model": model, "input": items}
    if system:
        params["instructions"] = system
    if tools:
        params["tools"] = [{
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object"}),
        } for t in tools]
    if max_tokens:
        params["max_output_tokens"] = max_tokens
    # Reasoning is native here and needs no override for tools to work, which is
    # the whole reason this endpoint exists for us — so nothing is sent.
    return params


def _translate_message_responses(message: dict[str, Any]) -> list[dict[str, Any]]:
    """One Anthropic-format message → one or more Responses API input items.

    tool_result blocks become standalone `function_call_output` items emitted
    before the user text they arrive with, mirroring _translate_message's
    ordering so a call stays adjacent to its output.

    An image is an `input_image` part here rather than chat-completions'
    `image_url` — same picture, different word, and the reason this function
    exists separately at all.
    """
    role = message["role"]
    content = message.get("content")
    if isinstance(content, str):
        ctype = "output_text" if role == "assistant" else "input_text"
        return [{"role": role, "content": [{"type": ctype, "text": content}]}]

    outputs: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []

    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "output_text" if role == "assistant" else "input_text",
                          "text": block.get("text", "")})
        elif btype == "image":
            url = _image_url(block) if role != "assistant" else None
            if url:
                images.append({"type": "input_image", "image_url": url})
        elif btype == "tool_use":
            calls.append({"type": "function_call", "call_id": block["id"],
                          "name": block["name"],
                          "arguments": json.dumps(block.get("input") or {})})
        elif btype == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, list):
                rc = "\n".join(p.get("text", "") for p in rc if isinstance(p, dict))
            outputs.append({"type": "function_call_output",
                            "call_id": block.get("tool_use_id", ""),
                            "output": rc if isinstance(rc, str) else str(rc)})

    out: list[dict[str, Any]] = []
    if role == "assistant":
        if parts:
            out.append({"role": "assistant", "content": parts})
        out.extend(calls)
    else:
        out.extend(outputs)
        # Images lead, as in _user_content and for the same reason.
        if parts or images:
            out.append({"role": role, "content": [*images, *parts]})
    return out


def _image_url(block: dict[str, Any]) -> str | None:
    """An Anthropic image block → the URL string this dialect wants.

    Both halves of the OpenAI-compatible family take an image as a URL and accept
    a `data:` URI as one, so a base64 block needs no upload step. Rendered here
    rather than in `warden/images.py` for the same reason tool_use is: this is
    the provider's dialect, and the transcript's own vocabulary should not have
    to know it.
    """
    source = block.get("source") or {}
    if source.get("type") == "base64":
        media_type = source.get("media_type") or "image/png"
        return f"data:{media_type};base64,{source.get('data', '')}"
    url = source.get("url")
    return str(url) if url else None


def _user_content(parts: list[str], images: list[dict[str, Any]]) -> Any:
    """The `content` field for a user message, in the narrowest shape that fits.

    Text alone stays a plain string — the shape every provider in this family
    has been receiving, and not worth restating as a one-element block list for
    the sake of symmetry when z.ai and Ollama are the ones who would find out.
    A picture forces the multimodal list, and only then.

    Images lead. Anthropic and OpenAI both report better grounding when the
    image precedes the question about it, and it costs nothing to honour here.
    """
    text = "\n\n".join(p for p in parts if p)
    if not images:
        return text
    blocks: list[dict[str, Any]] = []
    for image in images:
        url = _image_url(image)
        if url:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks


def _translate_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """One Anthropic-format message → one or more chat-completions messages.
    tool_result blocks become role:"tool" messages emitted before the user text,
    preserving their adjacency to the assistant tool_calls that produced them.

    An `image` block becomes an `image_url` part. It used to match none of the
    arms below and vanish here — the operator sent a screenshot, the request
    carried the sentence without it, and a model that cannot see is not thereby
    stopped from answering. If the selected model has no vision the provider now
    says so in an error, which is the outcome worth having."""
    role = message["role"]
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    tool_msgs: list[dict[str, Any]] = []
    user_parts: list[str] = []
    user_images: list[dict[str, Any]] = []
    assistant_text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_content: str | None = None

    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            (assistant_text if role == "assistant" else user_parts).append(block.get("text", ""))
        elif btype == "image":
            # Only ever on a user turn: no provider here returns image content,
            # and an assistant message carrying one could not be replayed.
            if role != "assistant":
                user_images.append(block)
        elif btype == "tool_use":
            tool_calls.append({
                "id": block["id"], "type": "function",
                "function": {"name": block["name"],
                             "arguments": json.dumps(block.get("input") or {})},
            })
            # DeepSeek requires the chain-of-thought to round-trip on the SAME
            # assistant message that carries the tool calls. It arrives on the
            # first tool_use block (all blocks in a turn share one reasoning
            # content); kept as the message-level field the provider expects.
            if reasoning_content is None and block.get("reasoning_content"):
                reasoning_content = block["reasoning_content"]
        elif btype == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, list):
                rc = "\n".join(p.get("text", "") for p in rc if isinstance(p, dict))
            tool_msgs.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                              "content": rc if isinstance(rc, str) else str(rc)})

    out: list[dict[str, Any]] = []
    if role == "assistant":
        text = "\n".join(t for t in assistant_text if t)
        if tool_calls:
            # `content` must be "" not null here — z.ai GLM rejects null content
            # (error 1214) mid-loop; "" is valid for every OpenAI-compat provider.
            msg = {"role": "assistant", "content": text, "tool_calls": tool_calls}
            if reasoning_content:
                # Message-level, sibling of `tool_calls`: DeepSeek rejects a
                # replayed tool call whose reasoning is missing.
                msg["reasoning_content"] = reasoning_content
            out.append(msg)
        elif text:
            out.append({"role": "assistant", "content": text})
    else:
        out.extend(tool_msgs)
        if user_parts or user_images:
            out.append({"role": role, "content": _user_content(user_parts, user_images)})
    return out


def _parse_tool_args(raw: str | None, tool_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("tool_args_unparseable", extra={"tool": tool_name})
        return {}


def _gen_tool_id() -> str:
    # Some compat layers (Gemini) omit tool-call ids; the loop needs one to pair
    # tool_use with tool_result. Generated, never hardcoded.
    return f"call_{uuid.uuid4().hex[:24]}"
