"""Wrote code, ran nothing.

The most expensive failure in an agentic loop is not a crash — it is a confident
report of work that was never executed. The model cannot catch it in itself: a
plan it never ran reads, from the inside, exactly like one it did. The loop can,
because it watched which tools were called and in what order.

So before a job ends, if files changed and nothing was run afterwards, the loop
asks once. Once, not repeatedly — "there is nothing to run here" is sometimes
the right answer, and the loop cannot know that but the agent can say it.
"""
from __future__ import annotations

import asyncio

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import _VERIFY_PROMPT, Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult

from tests.test_forge import Echo


class _Noop(Tool):
    description = "x" * 40
    Args = Echo.Args

    async def call(self, args, ctx) -> ToolResult:
        return ToolResult(content="ok")


class _Write(_Noop):
    name = "write_file"


class _Run(_Noop):
    name = "run_command"


def _ctx() -> ToolContext:
    return ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _warden(steps) -> Warden:
    return Warden(system_prompt="", tools={"write_file": _Write(), "run_command": _Run()},
                  model=ScriptedModel(steps), ctx=_ctx(), max_iterations=12)


def _asked(term) -> bool:
    return any(_VERIFY_PROMPT in str(m.get("content", "")) for m in term.messages)


def test_writing_then_stopping_is_questioned():
    """The whole point: an edit that was never run is not finished work."""
    steps = [
        lambda m: ("editing", [tool_call("write_file", text="x")]),
        lambda m: ("All done — the fix is in place.", []),
        lambda m: ("Ran the tests, they pass.", []),
    ]
    term = asyncio.run(_warden(steps).run("fix it"))

    assert _asked(term), "the loop accepted an unverified change as complete"
    assert term.reason is StopReason.COMPLETED


def test_running_after_writing_is_left_alone():
    """The agent did the right thing; interrupting it would be nagging."""
    steps = [
        lambda m: ("editing", [tool_call("write_file", text="x")]),
        lambda m: ("checking", [tool_call("run_command", text="pytest")]),
        lambda m: ("Tests pass.", []),
    ]
    term = asyncio.run(_warden(steps).run("fix it"))

    assert not _asked(term)
    assert term.reason is StopReason.COMPLETED


def test_running_BEFORE_the_edit_does_not_count():
    """Tests that passed before a change say nothing about the change. This is
    the ordering the check exists for — a naive 'did it ever run a command'
    would be satisfied here and let the edit through unverified."""
    steps = [
        lambda m: ("looking", [tool_call("run_command", text="pytest")]),
        lambda m: ("editing", [tool_call("write_file", text="x")]),
        lambda m: ("Done.", []),
        lambda m: ("Now verified.", []),
    ]
    term = asyncio.run(_warden(steps).run("fix it"))

    assert _asked(term)


def test_a_job_that_changed_nothing_is_not_questioned():
    """Answering a question is complete work. Demanding a test run for it would
    train the agent to ignore the ask."""
    steps = [lambda m: ("It is defined in engine.py.", [])]
    term = asyncio.run(_warden(steps).run("where is the loop?"))

    assert not _asked(term)
    assert term.reason is StopReason.COMPLETED


def test_the_question_is_asked_once_and_not_again():
    """A second ask is nagging, and 'there is nothing to run' is sometimes the
    correct answer to the first."""
    steps = [
        lambda m: ("editing", [tool_call("write_file", text="x")]),
        lambda m: ("Done.", []),
        lambda m: ("There is no test suite in this repo, so I could not run it.", []),
    ]
    term = asyncio.run(_warden(steps).run("fix it"))

    asked = sum(1 for m in term.messages if _VERIFY_PROMPT in str(m.get("content", "")))
    assert asked == 1
    assert term.reason is StopReason.COMPLETED


def test_the_ask_names_the_honest_way_out():
    """Otherwise it reads as 'run something' and the agent invents a command to
    satisfy it, which is worse than the silence it replaced."""
    assert "nothing to run" in _VERIFY_PROMPT
    assert "did NOT verify" in _VERIFY_PROMPT


def test_the_final_answer_is_the_verified_one():
    """The reply the operator reads must be the one written after checking, not
    the optimistic one that triggered the ask."""
    steps = [
        lambda m: ("editing", [tool_call("write_file", text="x")]),
        lambda m: ("All done.", []),
        lambda m: ("Ran pytest: 12 passed.", []),
    ]
    term = asyncio.run(_warden(steps).run("fix it"))

    assert term.final_text == "Ran pytest: 12 passed."
