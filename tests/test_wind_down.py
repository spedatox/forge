"""Hitting the ceiling should end the run, not behead it.

Stopping dead at the boundary throws away the one thing worth having: the agent
knows what it just did, what is half-finished and what it was about to do next,
and nobody else does. The operator is left reconstructing that from a scrolled-
past transcript, or — far more often — starting the whole job again.

So the ceiling now buys one final turn with no tools. The two properties that
have to hold together: it produces a handover, and it cannot use that turn to
keep working. A ceiling that can be talked past is not a ceiling.
"""
from __future__ import annotations

import asyncio

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult

from tests.test_forge import Echo


def _ctx() -> ToolContext:
    return ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _warden(steps, max_iter=3, signal=None, tools=None) -> Warden:
    return Warden(system_prompt="", tools=tools or {"echo": Echo()},
                  model=ScriptedModel(steps), ctx=_ctx(),
                  max_iterations=max_iter, signal=signal)


_FOREVER = [lambda m: ("again", [tool_call("echo", text="x")])] * 50


def test_the_ceiling_still_stops_the_run():
    """The guard itself is unchanged — this is a wind-down, not a reprieve."""
    term = asyncio.run(_warden(_FOREVER, max_iter=3).run("go"))
    assert term.reason is StopReason.MAX_ITERATIONS
    assert term.iterations == 3


def test_the_agent_gets_a_final_turn_to_hand_over():
    """The whole point: the run ends with a report rather than with silence."""
    steps = [*[lambda m: ("again", [tool_call("echo", text="x")])] * 3,
             lambda m: ("I edited hisar.jsx:1507; the info panel is untouched.", [])]
    term = asyncio.run(_warden(steps, max_iter=3).run("go"))

    assert term.reason is StopReason.MAX_ITERATIONS
    assert "info panel is untouched" in term.final_text


def test_the_handover_turn_is_asked_for_explicitly():
    """It has to know it is out of turns, or it writes another plan instead of
    a handover."""
    seen: list[list[dict]] = []

    def _capture(messages):
        seen.append([dict(m) for m in messages])
        return ("wrapping up", [])

    steps = [*[lambda m: ("again", [tool_call("echo", text="x")])] * 2, _capture]
    asyncio.run(_warden(steps, max_iter=2).run("go"))

    last_user = [m for m in seen[-1] if m["role"] == "user"][-1]
    text = str(last_user["content"]).lower()
    assert "last turn" in text and "handover" in text
    assert "did not get to" in text        # names the thing that makes it resumable


def test_the_final_turn_has_no_tools():
    """What makes this safe rather than a ceiling that does not hold. It cannot
    edit another file or run another command — it can only talk."""
    offered: list[list] = []

    class _Watching(ScriptedModel):
        async def stream(self, *, system, messages, tools, signal):
            offered.append(list(tools))
            async for ev in super().stream(system=system, messages=messages,
                                           tools=tools, signal=signal):
                yield ev

    warden = Warden(system_prompt="", tools={"echo": Echo()},
                    model=_Watching(_FOREVER), ctx=_ctx(), max_iterations=2)
    asyncio.run(warden.run("go"))

    assert offered[0], "the working turns should have tools"
    assert offered[-1] == [], "the handover turn was offered tools and could keep working"


def test_a_tool_call_in_the_handover_cannot_run():
    """Belt and braces: even if a provider ignores the empty tool list and the
    model asks anyway, the loop is left immediately — nothing executes."""
    ran: list[str] = []

    class _Counting(Tool):
        name = "echo"
        description = "x" * 40
        Args = Echo.Args

        async def call(self, args, ctx) -> ToolResult:
            ran.append("call")
            return ToolResult(content="ok")

    steps = [*[lambda m: ("again", [tool_call("echo", text="x")])] * 2,
             lambda m: ("one more", [tool_call("echo", text="sneaky")])]
    asyncio.run(_warden(steps, max_iter=2, tools={"echo": _Counting()}).run("go"))

    assert len(ran) == 2, "a tool ran during or after the handover turn"


def test_an_interrupted_run_is_not_charged_for_a_summary():
    """Someone who pressed ctrl+c is not waiting for a report, and spending
    another model call on one is the wrong answer to 'stop'."""
    calls = {"n": 0}

    class _Counting(ScriptedModel):
        async def stream(self, *, system, messages, tools, signal):
            calls["n"] += 1
            async for ev in super().stream(system=system, messages=messages,
                                           tools=tools, signal=signal):
                yield ev

    signal = asyncio.Event()
    signal.set()
    warden = Warden(system_prompt="", tools={"echo": Echo()},
                    model=_Counting(_FOREVER), ctx=_ctx(),
                    max_iterations=0, signal=signal)
    term = asyncio.run(warden.run("go"))

    assert term.reason is StopReason.MAX_ITERATIONS
    assert calls["n"] == 0, "a model call was made for a run the operator stopped"


def test_a_failed_handover_still_ends_cleanly():
    """The summary is a courtesy. Failing to get one must not turn a finished
    run into an error the caller has to handle differently."""
    class _Broken(ScriptedModel):
        async def stream(self, *, system, messages, tools, signal):
            if not tools:                      # the handover turn
                raise RuntimeError("provider fell over")
            async for ev in super().stream(system=system, messages=messages,
                                           tools=tools, signal=signal):
                yield ev

    warden = Warden(system_prompt="", tools={"echo": Echo()},
                    model=_Broken(_FOREVER), ctx=_ctx(), max_iterations=2)
    term = asyncio.run(warden.run("go"))

    assert term.reason is StopReason.MAX_ITERATIONS
