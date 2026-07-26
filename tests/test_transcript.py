"""Transcript repair (peer replay resilience).

The load-bearing case: a turn that dies after asking for tools leaves an
assistant tool_use with no matching tool_result. Replayed raw that is an API
400, and the agent 'forgets' the whole conversation instead of one botched turn.
"""
from forge.warden.transcript import repair_transcript


def _ids(content):
    return {b.get("tool_use_id") for b in content if b.get("type") == "tool_result"}


def test_dangling_tool_use_gets_a_synthetic_result():
    """The exact failure: a bunch of work, then the turn died before the result
    was recorded. The tool_use must be answered so the next turn can replay."""
    history = [
        {"role": "user", "content": "scan the host"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "running nmap"},
            {"type": "tool_use", "id": "t1", "name": "run_command", "input": {"cmd": "nmap"}},
        ]},
        # ← turn died here: no tool_result, then the operator sent a new message
        {"role": "user", "content": "what did you find?"},
    ]
    out = repair_transcript(history)
    # The assistant tool_use is now answered before the next user turn.
    assert out[1]["role"] == "assistant"
    assert out[2]["role"] == "user"
    assert "t1" in _ids(out[2]["content"])
    assert out[2]["content"][0]["is_error"] is True
    # And the real next user message is preserved, after the back-fill.
    assert out[-1]["content"] == "what did you find?"


def test_backfill_merges_into_an_existing_result_turn():
    """One of two parallel tool calls got a result, the other didn't — only the
    missing one is back-filled, and both live in the same user turn."""
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "x", "input": {}},
            {"type": "tool_use", "id": "b", "name": "y", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
        ]},
    ]
    out = repair_transcript(history)
    assert _ids(out[2]["content"]) == {"a", "b"}      # both answered
    assert len(out) == 3                               # merged, not appended


def test_orphan_tool_result_is_dropped():
    """A tool_result with no matching tool_use before it is invalid on its own."""
    history = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ghost", "content": "stale"},
            {"type": "text", "text": "still here"},
        ]},
    ]
    out = repair_transcript(history)
    assert len(out) == 1
    assert all(b.get("type") != "tool_result" for b in out[0]["content"])


def test_trims_to_start_and_end_on_user():
    history = [
        {"role": "assistant", "content": "leading assistant, dropped"},
        {"role": "user", "content": "real start"},
        {"role": "assistant", "content": "trailing assistant, dropped"},
    ]
    out = repair_transcript(history)
    assert [m["role"] for m in out] == ["user"]
    assert out[0]["content"] == "real start"


def test_foreign_and_empty_messages_are_dropped():
    history = [
        {"role": "system", "content": "nope"},
        {"role": "user", "content": ""},
        {"role": "user", "content": "keep me"},
        "not a dict",
    ]
    out = repair_transcript(history)
    assert out == [{"role": "user", "content": "keep me"}]


def test_clean_transcript_is_unchanged_in_shape():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]},
        {"role": "assistant", "content": "here is the answer"},
        {"role": "user", "content": "thanks, one more thing"},
    ]
    out = repair_transcript(history)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant", "user"]
    assert out == history       # nothing to fix → identical


def test_empty_history_stays_empty():
    assert repair_transcript([]) == []
