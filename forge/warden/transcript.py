"""Repairing a transcript before it is replayed.

A chat turn on the peer path is seeded with the whole prior conversation (Mark
VI's DB is the source of truth, resent every turn). That conversation is only as
well-formed as the turn that produced it — and a turn that DIED after doing work
does not produce a clean one. The Anthropic API is strict about two things this
module fixes:

  * every assistant ``tool_use`` block must be answered by a ``tool_result`` in
    the very next user message, and
  * the transcript must begin, and (to continue) end, on a user turn.

When a previous turn errored mid-flight — the provider dropped the stream after
the model asked for tools, the operator interrupted, the iteration ceiling hit —
the persisted transcript can carry an assistant ``tool_use`` with no matching
result. Replayed verbatim that is a 400, the new turn dies on arrival, and to the
operator the agent looks like it forgot everything it just did. Repairing the
transcript here means a botched turn costs its own output, not the whole
conversation: the loop can always pick up from a valid state.
"""
from __future__ import annotations

from typing import Any

# What a back-filled result says. It is marked is_error so the model treats the
# missing observation as a failure to retry, not as a success it can build on.
_ORPHAN_RESULT = "[no result recorded — the previous turn ended before this tool finished]"


def repair_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a transcript that is always safe to seed the loop with.

    Non-destructive where it can be: an unanswered ``tool_use`` is *answered*
    with a synthetic error result rather than deleted, so the model keeps the
    reasoning that led to the call. Only genuinely unusable pieces — foreign
    roles, empty messages, orphan results, a leading or trailing turn that would
    make the sequence invalid — are dropped."""
    msgs = _coerce(messages)
    msgs = _pair_tools(msgs)
    msgs = _trim_edges(msgs)
    return msgs


def _coerce(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only user/assistant messages that carry content, as {role, content}."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or content in (None, "", []):
            continue
        out.append({"role": role, "content": content})
    return out


def _tool_use_ids(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    return [b["id"] for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]


def _tool_result_ids(content: Any) -> set[str]:
    if not isinstance(content, list):
        return set()
    return {b["tool_use_id"] for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")}


def _pair_tools(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make every ``tool_use`` answered and drop every orphan ``tool_result``.

    Walks once. At an assistant turn that asked for tools, any id the following
    user turn does not answer gets a synthetic error result — merged into that
    user turn if there is one, or inserted as a fresh user turn if there is not.
    At a user turn, a ``tool_result`` whose id was not asked for by the assistant
    immediately before it is an orphan and is stripped."""
    out: list[dict[str, Any]] = []
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        if m["role"] == "assistant":
            out.append(m)
            use_ids = _tool_use_ids(m["content"])
            if use_ids:
                nxt = msgs[i + 1] if i + 1 < n else None
                answered = _tool_result_ids(nxt["content"]) if nxt and nxt["role"] == "user" else set()
                missing = [tid for tid in use_ids if tid not in answered]
                if missing:
                    synthetic = [{"type": "tool_result", "tool_use_id": tid,
                                  "content": _ORPHAN_RESULT, "is_error": True}
                                 for tid in missing]
                    if nxt is not None and nxt["role"] == "user" and isinstance(nxt["content"], list):
                        # Prepend the back-fills so all tool_result blocks stay
                        # together at the head of the user turn.
                        msgs[i + 1] = {"role": "user", "content": synthetic + list(nxt["content"])}
                    else:
                        out.append({"role": "user", "content": synthetic})
            i += 1
            continue

        # user turn: drop tool_result blocks the preceding assistant never asked for
        content = m["content"]
        prev = out[-1] if out else None
        expected = set(_tool_use_ids(prev["content"])) if prev and prev["role"] == "assistant" else set()
        if isinstance(content, list):
            kept = [b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") not in expected)]
            if not kept:
                i += 1          # became empty — skip it entirely
                continue
            m = {"role": "user", "content": kept}
        out.append(m)
        i += 1
    return out


def _trim_edges(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A transcript must start and end on a user turn to be continued from.

    Leading assistant turns (and any tool_result-only user turn stranded by
    dropping them) go; trailing assistant turns go. Anything left is a valid
    sequence the model can answer."""
    start = 0
    while start < len(msgs) and msgs[start]["role"] != "user":
        start += 1
    end = len(msgs)
    while end > start and msgs[end - 1]["role"] != "user":
        end -= 1
    return msgs[start:end]
