"""Subagents — delegation with its own context, and a bounded reach.

The feature is context isolation, not parallelism: a search that costs forty
file reads costs the parent one paragraph if a subagent does it. What has to be
tested is the part that is invisible when it breaks — that a child cannot spawn
children, that a read-only specialist genuinely has no way to write, and that a
child which fails hands back a value rather than taking the parent down with it.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tools import ALL_TOOLS, CODING_TOOLS, SECURITY_TOOLS
from forge.tools.task import TaskArgs, TaskTool
from forge.warden.state import StopReason, Terminal
from forge.warden.subagents import (
    BUILT_INS, MAX_CONCURRENT, SUBAGENT_MAX_ITERATIONS, SubagentRunner, spec_for,
)


class _Tool:
    """Stand-in for a real tool; only its name matters to the allowlist."""

    def __init__(self, name):
        self.name = name


PARENT_TOOLS = {
    n: _Tool(n) for n in
    ("read_file", "write_file", "edit_file", "run_command", "glob", "grep",
     "graph_query", "graph_path", "graph_overview", "todo_write", "task")
}


class _FakeWarden:
    def __init__(self, terminal, record=None, **kw):
        self._terminal = terminal
        self.kw = kw
        self._record = record

    async def run(self, task):
        if self._record is not None:
            self._record.append(task)
        return self._terminal


def _runner(terminal=None, record=None, tools=None):
    terminal = terminal or Terminal(reason=StopReason.COMPLETED, final_text="done")
    captured: dict = {}

    def build(**kw):
        captured.update(kw)
        return _FakeWarden(terminal, record, **kw)

    r = SubagentRunner(build_warden=build,
                       parent_tools=lambda: dict(tools or PARENT_TOOLS))
    r.captured = captured        # type: ignore[attr-defined]
    return r


# ── Depth one ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(BUILT_INS))
def test_no_subagent_can_spawn_subagents(kind):
    """Fan-out has to be bounded by what the parent asked for. Recursion looks
    reasonable at every individual step and is unbounded in aggregate."""
    runner = _runner()
    assert "task" not in runner.tools_for(BUILT_INS[kind])


def test_a_general_subagent_otherwise_inherits_the_parents_tools():
    runner = _runner()
    tools = runner.tools_for(BUILT_INS["general"])
    assert set(tools) == set(PARENT_TOOLS) - {"task"}


# ── The allowlist IS the reach ───────────────────────────────────────────────


@pytest.mark.parametrize("forbidden", ["write_file", "edit_file", "run_command"])
def test_explore_has_no_way_to_modify_anything(forbidden):
    """Not 'is told not to' — the tool is absent from its dict, so a model that
    decides the instructions are advisory still cannot call it."""
    assert forbidden not in _runner().tools_for(BUILT_INS["explore"])


def test_explore_can_still_search_and_read():
    tools = _runner().tools_for(BUILT_INS["explore"])
    assert {"read_file", "grep", "glob"} <= set(tools)


def test_review_may_run_commands_but_not_write():
    """It needs to run the tests to review honestly; it must not fix them."""
    tools = _runner().tools_for(BUILT_INS["review"])
    assert "run_command" in tools
    assert "write_file" not in tools and "edit_file" not in tools


def test_a_missing_parent_tool_is_dropped_not_invented():
    """A deployment without the graph must not hand the child a broken name."""
    runner = _runner(tools={"read_file": _Tool("read_file")})
    assert set(runner.tools_for(BUILT_INS["explore"])) == {"read_file"}


# ── A child that fails must not take the parent with it ──────────────────────


def test_an_exception_in_the_child_comes_back_as_a_value():
    def build(**kw):
        class _Boom:
            async def run(self, task):
                raise RuntimeError("model stream died")
        return _Boom()

    runner = SubagentRunner(build_warden=build, parent_tools=lambda: PARENT_TOOLS)
    report, failed = asyncio.run(runner.run(BUILT_INS["explore"], "find it"))

    assert failed
    assert "model stream died" in report


def test_an_errored_child_reports_the_error():
    runner = _runner(Terminal(reason=StopReason.ERROR, error="context overflow"))
    report, failed = asyncio.run(runner.run(BUILT_INS["general"], "do it"))
    assert failed and "context overflow" in report


def test_an_interrupted_child_is_not_dressed_up_as_a_finding():
    runner = _runner(Terminal(reason=StopReason.ABORTED, final_text="got halfway"))
    report, failed = asyncio.run(runner.run(BUILT_INS["explore"], "find it"))
    assert failed
    assert "interrupted" in report.lower() and "got halfway" in report


def test_hitting_the_ceiling_returns_partial_work_marked_partial():
    """Partial findings beat nothing, as long as the parent is not told they
    are complete."""
    runner = _runner(Terminal(reason=StopReason.MAX_ITERATIONS,
                              final_text="found three of them"))
    report, failed = asyncio.run(runner.run(BUILT_INS["explore"], "find them"))

    assert not failed                       # there IS a usable result
    assert "found three of them" in report
    assert "partial" in report.lower()


def test_a_silent_child_is_an_error_not_a_success():
    """An empty report read as success is how a parent concludes work happened
    when none did."""
    runner = _runner(Terminal(reason=StopReason.COMPLETED, final_text="   "))
    report, failed = asyncio.run(runner.run(BUILT_INS["general"], "do it"))
    assert failed and "not as success" in report


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_the_child_gets_the_lower_iteration_ceiling():
    runner = _runner()
    asyncio.run(runner.run(BUILT_INS["explore"], "find it"))
    assert runner.captured["max_iterations"] == SUBAGENT_MAX_ITERATIONS


def test_the_child_gets_its_specialists_prompt_not_the_parents():
    runner = _runner()
    asyncio.run(runner.run(BUILT_INS["review"], "review it"))
    assert runner.captured["system_prompt"] == BUILT_INS["review"].system_prompt


def test_the_prompt_is_what_the_child_is_asked_to_do():
    seen: list[str] = []
    runner = _runner(record=seen)
    asyncio.run(runner.run(BUILT_INS["explore"], "where is retry logic"))
    assert seen == ["where is retry logic"]


def test_concurrency_is_capped():
    """One turn may emit several task calls and the loop runs parallel-safe
    tools concurrently; without a cap that is unbounded model streams."""
    assert 1 <= MAX_CONCURRENT <= 8


# ── The tool surface ─────────────────────────────────────────────────────────


def _ctx(runner):
    class _Ctx:
        subagents = runner
    return _Ctx()


def test_the_tool_returns_the_childs_report():
    tool = TaskTool()
    result = asyncio.run(tool.call(
        TaskArgs(description="find it", prompt="where is retry", subagent_type="explore"),
        _ctx(_runner()),
    ))
    assert result.content == "done" and not result.is_error


def test_an_unknown_type_names_the_ones_that_exist():
    tool = TaskTool()
    result = asyncio.run(tool.call(
        TaskArgs(description="x", prompt="y", subagent_type="architect"),
        _ctx(_runner()),
    ))
    assert result.is_error
    for known in BUILT_INS:
        assert known in result.content


def test_an_empty_prompt_is_refused_before_a_model_is_paid_for():
    tool = TaskTool()
    result = asyncio.run(tool.call(
        TaskArgs(description="x", prompt="   ", subagent_type="general"),
        _ctx(_runner()),
    ))
    assert result.is_error


def test_no_runner_wired_tells_the_model_to_do_it_itself():
    """Rather than raising: the model can act on this, and cannot act on a
    traceback."""
    class _Bare:
        pass

    tool = TaskTool()
    result = asyncio.run(tool.call(
        TaskArgs(description="x", prompt="do it"), _Bare()))
    assert result.is_error and "yourself" in result.content


def test_the_default_type_is_general():
    assert TaskArgs(description="x", prompt="y").subagent_type == "general"


def test_every_type_is_described_for_selection():
    """Type choice is the model's, and it makes it from these strings alone."""
    for spec in BUILT_INS.values():
        assert len(spec.description) > 120, f"{spec.name} is under-described"
        assert "not" in spec.description.lower(), \
            f"{spec.name} never says when NOT to use it"


def test_lookup_is_forgiving_of_case_and_padding():
    assert spec_for("  Explore ") is BUILT_INS["explore"]
    assert spec_for("nope") is None


# ── Registration ─────────────────────────────────────────────────────────────


def test_task_is_a_coding_tool_and_not_a_security_one():
    """Centurion runs security tooling against live targets; handing it a
    general-purpose spawner widens that blast radius for no benefit."""
    assert TaskTool in CODING_TOOLS
    assert TaskTool not in SECURITY_TOOLS
    assert ALL_TOOLS["task"] is TaskTool


# ── A subagent must not speak for the parent ─────────────────────────────────


def _emitting_runner(child_events):
    """A runner whose child emits `child_events`, capturing what escapes."""
    escaped: list[dict] = []

    async def parent_emit(ev):
        escaped.append(ev)

    class _Emitting:
        def __init__(self, **kw):
            self._emit = kw["emit"]

        async def run(self, task):
            for ev in child_events:
                await self._emit(ev)
            return Terminal(reason=StopReason.COMPLETED, final_text="report")

    r = SubagentRunner(build_warden=lambda **kw: _Emitting(**kw),
                       parent_tools=lambda: dict(PARENT_TOOLS),
                       emit=parent_emit)
    return r, escaped


def test_a_finished_subagent_does_not_end_the_parents_turn():
    """The bug, 2026-08-06: a child Warden emits `done` when it completes, and
    it was handed the parent's emit. That frame reached the client, which reads
    `done` as end-of-turn — Heartbreaker closed the stream the moment the
    subagent finished, while the parent kept working invisibly and kept
    billing the provider."""
    runner, escaped = _emitting_runner([{"type": "done", "data": "child done"}])
    asyncio.run(runner.run(BUILT_INS["explore"], "find it"))

    assert not any(e["type"] == "done" for e in escaped), \
        "a subagent's `done` escaped and will close the parent's stream"


def test_a_failing_subagent_does_not_error_the_parents_turn():
    """Its failure is reported to the parent as a tool result it can react to."""
    runner, escaped = _emitting_runner([{"type": "error", "data": "child broke"}])
    asyncio.run(runner.run(BUILT_INS["general"], "do it"))

    assert not any(e["type"] == "error" for e in escaped)


def test_a_subagents_prose_is_not_the_parents_answer():
    """Its report comes back as the tool result. Streaming it as prose is what
    made a subagent's write-up appear as Optimus's own reply."""
    runner, escaped = _emitting_runner([{"type": "chunk", "data": "I looked at 40 files..."}])
    asyncio.run(runner.run(BUILT_INS["explore"], "find it"))

    assert not any(e["type"] == "chunk" for e in escaped)


def test_tool_activity_is_forwarded_so_it_does_not_look_hung():
    """A two-minute delegation with no output is indistinguishable from a
    crash, so the work IS shown — tagged, so it is not mistaken for the
    parent's own calls."""
    runner, escaped = _emitting_runner([
        {"type": "tool", "data": {"id": "t1", "name": "grep", "input": {}}},
    ])
    asyncio.run(runner.run(BUILT_INS["explore"], "find it"))

    assert len(escaped) == 1
    assert escaped[0]["type"] == "tool"
    assert escaped[0]["data"]["name"] == "explore:grep"


def test_the_report_still_reaches_the_caller_as_a_value():
    """Silencing the stream must not silence the result."""
    runner, _ = _emitting_runner([{"type": "done", "data": "x"}])
    report, failed = asyncio.run(runner.run(BUILT_INS["explore"], "find it"))
    assert report == "report" and not failed
