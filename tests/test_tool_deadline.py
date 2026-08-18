"""Every tool gets a wall clock, and the loop cannot be parked inside one.

`run_command` was the only bounded tool. Everything else — an MCP call over a
stdio pipe whose server stopped answering, a graph sidecar mid-index, a `web`
fetch whose socket died without closing, any tool written later by someone who
did not know to add one — could await forever, and `dispatch_tool` is awaited by
the engine, so forever means the whole job.

The shape borrowed from DSH's timeout-policy: the budget is DECLARED by the tool
and ENFORCED centrally. A tool cannot forget it, because it inherits one by
existing; a tool that genuinely must run longer says so with `SELF_BOUNDED` and
has to justify it in the same breath.

What these tests protect is the ordering as much as the mechanism. A backstop
that fires before the real timeout is worse than no backstop: it reports the
wrong cause for a command that was about to be stopped correctly anyway.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell
from forge.tools.claude_code import ClaudeCode
from forge.tools.shell import RunCommand, RunCommandArgs
from forge.tools.task import TaskTool
from forge.warden.dispatch import dispatch_tool
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.tool import (
    BACKSTOP_GRACE_S, DEFAULT_TOOL_TIMEOUT_S, SELF_BOUNDED, Tool, ToolContext,
    ToolResult, cell_backed_timeout)


@pytest.fixture
def ctx(tmp_path):
    cell = SubprocessCell(workspace=tmp_path, policy=CellPolicy())
    asyncio.run(cell.start())
    return ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


class Empty(BaseModel):
    pass


class Wedged(Tool):
    """A tool that never returns. The thing the backstop exists for."""
    name = "wedged"
    description = "hangs"
    Args = Empty
    READ_ONLY = True
    TIMEOUT_S = 0.05

    def __init__(self) -> None:
        self.cancelled = False

    async def call(self, args, ctx):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult("unreachable")


# ── the deadline fires, and says something usable ────────────────────────────


def test_a_wedged_tool_is_cancelled_rather_than_awaited_forever(ctx):
    tool = Wedged()
    res = asyncio.run(dispatch_tool({"wedged": tool}, "wedged", {}, ctx))

    assert res.is_error
    assert "TOOL_TIMEOUT" in res.content
    assert tool.cancelled, "the tool's coroutine must actually be cancelled"


def test_the_timeout_says_the_arguments_were_not_the_problem(ctx):
    """A model told only 'timed out' retries with a narrower argument, which
    cannot help — the arguments already passed validation — and costs another
    full deadline to discover that."""
    res = asyncio.run(dispatch_tool({"wedged": Wedged()}, "wedged", {}, ctx))

    assert "not a fault in your arguments" in res.content
    assert "retrying the same call unchanged" in res.content


def test_the_timeout_warns_that_half_the_work_may_have_landed(ctx):
    """A cancelled write is not a no-op. An agent that assumes otherwise builds
    on a file it never looked at."""
    res = asyncio.run(dispatch_tool({"wedged": Wedged()}, "wedged", {}, ctx))

    assert "is still done" in res.content or "still done" in res.content
    assert "check the state" in res.content


def test_a_tool_that_finishes_in_time_is_untouched(ctx):
    class Quick(Wedged):
        name = "quick"
        TIMEOUT_S = 5.0

        async def call(self, args, ctx):
            return ToolResult("done")

    res = asyncio.run(dispatch_tool({"quick": Quick()}, "quick", {}, ctx))
    assert not res.is_error
    assert res.content == "done"


# ── opting out has to be deliberate, and rare ────────────────────────────────


def test_self_bounded_tools_are_not_wrapped(ctx):
    """`task` opts out because a subagent's own ceiling — and the handover its
    wind-down writes — is a better stopping condition than a wall clock, which
    would throw that handover away at an arbitrary second."""
    assert TaskTool.TIMEOUT_S == SELF_BOUNDED


def test_every_other_tool_inherits_a_bound_by_existing():
    """The fail-closed direction: a new tool is bounded unless it says
    otherwise, rather than unbounded unless it remembers."""
    class Fresh(Tool):
        name = "fresh"
        description = "a tool nobody thought about timeouts for"
        Args = Empty

        async def call(self, args, ctx):
            return ToolResult("")

    assert Fresh.TIMEOUT_S == DEFAULT_TOOL_TIMEOUT_S
    assert Fresh.TIMEOUT_S != SELF_BOUNDED


# ── the ordering, which is the part that silently rots ───────────────────────


def test_the_backstop_sits_above_the_cell_ceiling(ctx):
    """The Cell is the timeout that should fire: it kills the process group, so
    the command actually stops, and it names the honest cause. Inverted, the
    backstop cuts off legitimate long commands and blames the harness."""
    limit = RunCommand().timeout_s(RunCommandArgs(command="sleep 1"), ctx)

    assert limit > ctx.cell.policy.max_timeout_s
    assert limit == ctx.cell.policy.max_timeout_s + BACKSTOP_GRACE_S


def test_no_cell_backed_tool_ties_with_the_default(tmp_path):
    """Centurion's profile sets the Cell to 300s. A flat 300s backstop would
    have been an exact tie on the default agent — two clocks, nothing deciding
    which wins, and a timeout nobody can attribute from the message."""
    cell = SubprocessCell(workspace=tmp_path,
                          policy=CellPolicy(default_timeout_s=300))
    c = ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                    permissions=PermissionEngine(), network_allowed=False)

    assert cell_backed_timeout(c) != DEFAULT_TOOL_TIMEOUT_S
    assert cell_backed_timeout(c) > cell.policy.default_timeout_s


def test_diagnostics_is_sized_for_its_whole_fallback_chain(ctx):
    """`_run_checker` tries each route until one answers, each a Cell command
    with the profile's own wall clock. Sized for one run, the backstop cancels
    the tool partway through behaving exactly as designed."""
    from forge.tools.diagnostics import _CHECKERS, Diagnostics, DiagnosticsArgs

    limit = Diagnostics().timeout_s(DiagnosticsArgs(path="."), ctx)

    assert limit > len(_CHECKERS) * ctx.cell.policy.default_timeout_s
    assert len(_CHECKERS) > 1, "the point of the test is the chain"


def test_ask_operator_outlives_the_owners_own_clock(tmp_path):
    """`FORGE_ASK_TIMEOUT_S` is how long the person gets to answer, and a
    Telegram owner reasonably gets many minutes. Cancelling the park partway
    through would report a hang for the one thing the tool exists to do."""
    from forge.tools.ask import AskOperator, AskOperatorArgs
    from forge.warden.oracle import ChannelOracle

    async def _send(_frame):
        return None

    cell = SubprocessCell(workspace=tmp_path, policy=CellPolicy())
    c = ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                    permissions=PermissionEngine(), network_allowed=False,
                    oracle=ChannelOracle(_send, timeout_s=900.0))

    limit = AskOperator().timeout_s(AskOperatorArgs(question="which?"), c)
    assert limit > 900.0


def test_a_raised_cell_ceiling_carries_the_backstop_with_it(tmp_path):
    """The reason this is a method and not a constant. An agent profile that
    gives its Cell a longer leash would silently invert the ordering against any
    fixed number."""
    cell = SubprocessCell(workspace=tmp_path,
                          policy=CellPolicy(max_timeout_s=3_600))
    ctx = ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                      permissions=PermissionEngine(), network_allowed=False)

    limit = RunCommand().timeout_s(RunCommandArgs(command="sleep 1"), ctx)
    assert limit > 3_600


def test_claude_code_outranks_its_own_inner_timeout():
    from forge.tools.claude_code import FCC_TIMEOUT_S

    assert ClaudeCode.TIMEOUT_S > FCC_TIMEOUT_S


# ── a tool's own timeout is not the harness's ────────────────────────────────


def test_a_tools_own_timeout_is_not_reported_as_the_harness_deadline(ctx):
    """On 3.11+ `asyncio.TimeoutError` IS `TimeoutError`, so a tool whose inner
    deadline escapes is indistinguishable from the dispatcher's by type alone.
    Reported as ours it would state a limit that never applied — the graph
    sidecar stops at 30s and the model would be told 300 — and a number stated
    confidently and wrongly is worse than none, because it gets planned against.
    """
    class InnerTimeout(Wedged):
        name = "inner"
        TIMEOUT_S = 30.0        # never reached; the tool gives up first

        async def call(self, args, ctx):
            raise TimeoutError("the sidecar stopped answering after 30s")

    res = asyncio.run(dispatch_tool({"inner": InnerTimeout()}, "inner", {}, ctx))

    assert res.is_error
    assert "TOOL_TIMEOUT" not in res.content, "attributed to the harness's clock"
    assert "fault inside inner itself" in res.content
    assert "sidecar stopped answering" in res.content


# ── fail-closed, the same rule the other safety answers follow ───────────────


def test_a_tool_whose_timeout_lookup_raises_still_gets_a_bound(ctx):
    """A tool that cannot answer a safety question gets the restrictive answer,
    not the permissive one — the rule `_parallel_safe` already follows."""
    class Broken(Wedged):
        name = "broken"

        def timeout_s(self, args, ctx):
            raise RuntimeError("no idea")

    from forge.warden.dispatch import _deadline

    assert _deadline(Broken(), Empty(), ctx) == DEFAULT_TOOL_TIMEOUT_S


def test_shadowing_the_method_with_a_value_is_refused():
    """`TIMEOUT_S = 30` on a class that means `timeout_s` is the mistake the
    other three flags already guard against; the failure mode is identical."""
    with pytest.raises(TypeError, match="timeout_s"):
        class Wrong(Tool):
            name = "wrong"
            description = ""
            Args = Empty
            timeout_s = 30.0

            async def call(self, args, ctx):
                return ToolResult("")
