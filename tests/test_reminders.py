"""Nudges the loop injects into itself.

The system prompt is read once, then competes with everything that has happened
since. A reminder arrives attached to the tool results that made it true, which
is why it lands when the prompt did not.

Each rule here answers a failure that actually happened in this repository, so
each test names the incident rather than a principle.
"""
from __future__ import annotations

from dataclasses import dataclass

from forge.warden import reminders
from forge.warden.reminders import ReminderState, due, observe
from forge.warden.state import LoopState


@dataclass
class _Use:
    name: str
    input: dict


@dataclass
class _Res:
    is_error: bool = False


def _loop(i):
    s = LoopState(messages=[])
    s.iteration = i
    return s


def _run(pairs_by_iteration) -> ReminderState:
    """Replay iterations, returning the state and any reminder fired last."""
    st = ReminderState()
    fired = None
    for i, pairs in enumerate(pairs_by_iteration, start=1):
        uses = [_Use(n, a) for n, a, _ in pairs]
        results = [_Res(e) for _, _, e in pairs]
        observe(st, _loop(i), uses, results)
        # Keep the last reminder that actually fired: a rule fires once and
        # returns None on every iteration after, so overwriting with None
        # would hide the thing under test.
        got = due(st)
        if got is not None:
            fired = got
    st.last_fired = fired          # type: ignore[attr-defined]
    return st


# ── the same call, failing, over and over ────────────────────────────────────


def test_a_repeated_identical_failure_is_called_out():
    """Observed: graph_path was called three times with identical arguments,
    because the error text recommended a parameter the tool did not expose.
    Nothing in the loop noticed it was stuck."""
    st = _run([[("read_file", {"path": "x"}, False)]] * 5
              + [[("graph_path", {"a": 1}, True)]] * 2)

    assert "graph_path" in st.last_fired
    assert "same arguments" in st.last_fired


def test_it_names_the_possibility_that_no_retry_will_work():
    """The specific trap: the error suggested an argument that did not exist,
    so every retry was doomed and the text never said so."""
    st = _run([[("read_file", {}, False)]] * 5 + [[("graph_path", {}, True)]] * 2)
    assert "no retry will ever work" in st.last_fired


def test_two_different_failures_are_not_a_repeat():
    """An agent adapting its arguments is working, not stuck."""
    st = _run([[("read_file", {}, False)]] * 5
              + [[("grep", {"p": "a"}, True)], [("grep", {"p": "b"}, True)]])
    assert st.last_fired is None


def test_a_success_clears_the_streak():
    """Reminding someone about a problem they just solved is noise, and noise
    is how a reminder system stops being read."""
    st = _run([[("read_file", {}, False)]] * 5
              + [[("grep", {"p": "a"}, True)], [("grep", {"p": "a"}, False)]])
    assert st.repeated_failure is None


# ── edits nobody ran ─────────────────────────────────────────────────────────


def test_editing_without_running_anything_is_flagged():
    st = _run([[("read_file", {}, False)]] * 5
              + [[("edit_file", {}, False)]]
              + [[("read_file", {}, False)]] * 3)

    assert "not run anything" in st.last_fired


def test_running_something_after_the_edit_says_nothing():
    """A reminder that fires while the thing is already happening says the
    harness is not watching."""
    st = _run([[("read_file", {}, False)]] * 5
              + [[("edit_file", {}, False)], [("run_command", {}, False)]]
              + [[("read_file", {}, False)]] * 3)

    assert st.last_fired is None


def test_diagnostics_counts_as_checking():
    st = _run([[("read_file", {}, False)]] * 5
              + [[("edit_file", {}, False)], [("diagnostics", {}, False)]]
              + [[("read_file", {}, False)]] * 3)
    assert st.last_fired is None


def test_a_failed_edit_is_not_a_change_to_verify():
    """It did not land, so there is nothing to run."""
    st = _run([[("read_file", {}, False)]] * 5
              + [[("edit_file", {}, True)]]
              + [[("read_file", {}, False)]] * 3)
    assert st.last_fired is None


# ── a long job with no plan ──────────────────────────────────────────────────


def test_a_long_job_without_a_plan_is_prompted():
    st = _run([[("read_file", {}, False)]] * 13)
    assert "no plan recorded" in st.last_fired
    assert "todo_write" in st.last_fired


def test_a_job_that_wrote_a_plan_is_left_alone():
    st = _run([[("todo_write", {}, False)]] + [[("read_file", {}, False)]] * 13)
    assert st.last_fired is None


# ── restraint ────────────────────────────────────────────────────────────────


def test_nothing_fires_while_the_loop_is_still_settling_in():
    """Three iterations in with no plan is reading, not disorganisation."""
    st = _run([[("read_file", {}, False)]] * 3)
    assert st.last_fired is None


def test_each_rule_fires_at_most_once():
    """A rule that repeats teaches the reader to skip the block — and then the
    one that mattered is skipped too."""
    st = ReminderState()
    for i in range(1, 20):
        observe(st, _loop(i), [_Use("read_file", {})], [_Res(False)])
        due(st)

    assert st.fired == {"no_plan"}


def test_only_one_reminder_arrives_per_turn():
    """Two at once reads as the harness panicking."""
    st = ReminderState()
    fired = []
    for i in range(1, 20):
        observe(st, _loop(i), [_Use("edit_file", {}), _Use("grep", {"p": 1})],
                [_Res(False), _Res(True)])
        got = due(st)
        if got:
            fired.append(got)
    assert all(f.count("<system-reminder>") == 1 for f in fired)


def test_the_most_expensive_failure_is_reported_first():
    """Being stuck in a retry loop costs more than an unwritten plan."""
    assert reminders.REMINDERS[0].name == "repeated_failure"


def test_a_reminder_is_wrapped_so_it_reads_as_the_system_speaking():
    st = _run([[("read_file", {}, False)]] * 13)
    assert st.last_fired.startswith("<system-reminder>")
    assert st.last_fired.endswith("</system-reminder>")


def test_the_reminder_reaches_the_model():
    """Wired, not merely written. A mechanism that is never reached is prose
    with extra steps — the same check that embarrassed the verify subagent's
    first draft."""
    import asyncio

    from forge.model.scripted import ScriptedModel, tool_call
    from forge.warden.engine import Warden
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.tool import Tool, ToolContext, ToolResult

    from tests.test_forge import Echo

    class _Failing(Tool):
        name = "grep"
        description = "x" * 40
        Args = Echo.Args

        async def call(self, args, ctx) -> ToolResult:
            return ToolResult(content="no match", is_error=True)

    ctx = ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                      permissions=PermissionEngine(), network_allowed=False)
    # The same failing call, over and over — the shape observed with graph_path.
    steps = [lambda m: ("trying", [tool_call("grep", text="same")])] * 12
    warden = Warden(system_prompt="", tools={"grep": _Failing()},
                    model=ScriptedModel(steps), ctx=ctx, max_iterations=10)

    term = asyncio.run(warden.run("find it"))
    transcript = str(term.messages)

    assert "<system-reminder>" in transcript
    assert "same arguments" in transcript
