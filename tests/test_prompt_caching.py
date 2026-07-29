"""Prompt-caching breakpoints on the Anthropic path.

The Warden's shape makes caching load-bearing rather than an optimisation: one
job runs up to `max_iterations` laps, each lap re-sends the whole system prompt,
tool array and transcript, and the ledger does not consider compacting until the
prompt nears 170 K tokens. Uncached that is a six-figure prefix billed at full
rate thirty times over — so these assert the breakpoints exist, sit on the right
blocks, and respect Anthropic's TTL-ordering rule.
"""
from forge.model.anthropic_model import (_PREFIX_TTL, _CONVERSATION_TTL,
                                         _cached_messages, _cached_system,
                                         _cached_tools)


def test_system_prompt_is_one_cached_block():
    out = _cached_system("you are a coding agent")
    assert out == [{
        "type": "text",
        "text": "you are a coding agent",
        "cache_control": {"type": "ephemeral", "ttl": _PREFIX_TTL},
    }]


def test_empty_system_is_passed_through_untouched():
    # The API rejects an empty text block — a job with no system prompt must not
    # be turned into an invalid request by the caching layer.
    assert _cached_system("") == ""


def test_only_the_last_tool_is_marked():
    tools = [{"name": "read"}, {"name": "write"}, {"name": "bash"}]
    out = _cached_tools(tools)
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[1]
    assert out[-1]["cache_control"]["ttl"] == _PREFIX_TTL
    # The caller's list must not be mutated — the engine reuses it across laps.
    assert all("cache_control" not in t for t in tools)


def test_empty_tools_is_left_alone():
    assert _cached_tools([]) == []


def test_transcript_breakpoint_lands_on_the_final_block():
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
        ]},
    ]
    out = _cached_messages(messages)
    assert "cache_control" not in str(out[0])
    assert "cache_control" not in str(out[1])
    assert out[-1]["content"][-1]["cache_control"]["ttl"] == _CONVERSATION_TTL
    assert messages[-1]["content"][-1] == {
        "type": "tool_result", "tool_use_id": "t1", "content": "done",
    }  # original untouched


def test_string_content_is_promoted_so_the_marker_has_somewhere_to_live():
    out = _cached_messages([{"role": "user", "content": "hello"}])
    assert out[0]["content"] == [{
        "type": "text", "text": "hello",
        "cache_control": {"type": "ephemeral", "ttl": _CONVERSATION_TTL},
    }]


def test_unmarkable_transcript_tail_is_not_corrupted():
    # Empty content has nothing to hang a breakpoint on; losing the cache is
    # acceptable, emitting a malformed message is not.
    messages = [{"role": "user", "content": []}]
    assert _cached_messages(messages) == [{"role": "user", "content": []}]
    assert _cached_messages([]) == []


def test_ttl_ordering_rule_is_satisfied_by_construction():
    """Anthropic requires longer-lived breakpoints to render BEFORE shorter-lived
    ones. Request order is [tools, system, messages], so pairing the long TTL
    with the first two and the short one with the transcript can never violate
    it — this pins that pairing so a future edit can't silently invert it."""
    assert _PREFIX_TTL == "1h"
    assert _CONVERSATION_TTL == "5m"
    tools_ttl = _cached_tools([{"name": "x"}])[-1]["cache_control"]["ttl"]
    system_ttl = _cached_system("s")[0]["cache_control"]["ttl"]
    convo_ttl = (
        _cached_messages([{"role": "user", "content": "m"}])[-1]["content"][-1]
        ["cache_control"]["ttl"]
    )
    assert tools_ttl == system_ttl == _PREFIX_TTL
    assert convo_ttl == _CONVERSATION_TTL


def test_breakpoint_budget_stays_within_the_api_maximum():
    # Anthropic allows at most 4 cache_control breakpoints per request; the
    # Warden spends exactly 3 (tools, system, transcript tail).
    n = (
        sum(1 for t in _cached_tools([{"name": "a"}, {"name": "b"}]) if "cache_control" in t)
        + sum(1 for b in _cached_system("s") if "cache_control" in b)
        + 1
    )
    assert n == 3
