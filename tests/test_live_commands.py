"""Watching a command instead of waiting for it, and being able to stop it.

Three things that only make sense together, which is why they are one file.

An operator reported that commands kept dying at the profile's 300s wall clock
and that ctrl+c killed Forge rather than the turn. Both are here, plus the piece
that makes the first one fixable at all: if the harness is going to stop killing
long commands, the operator has to be able to SEE one running and STOP it. Ship
any one of the three alone and you get a worse harness than before —

- streaming without the interrupt: you watch a command you cannot stop
- no-kill without streaming: a freeze with no explanation
- the interrupt without either: nothing to interrupt, because it already died
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from forge.cell.base import CellPolicy
from forge.cell.stream import Retained
from forge.cell.subprocess_cell import SubprocessCell

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None and __import__("os").name == "nt",
    reason="needs a POSIX shell",
)


# ── seeing it happen ─────────────────────────────────────────────────────────


def test_output_arrives_before_the_command_finishes():
    """The whole point. A five-minute build that shows nothing for five minutes
    is indistinguishable from a hang, and an operator cannot make a decision
    about something they cannot see."""
    seen: list[tuple[str, float]] = []
    started = time.monotonic()

    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()), CellPolicy())
        await cell.start()
        return await cell.run(
            "echo first; sleep 1; echo second",
            timeout=20,
            on_output=lambda s, t: seen.append((t, time.monotonic() - started)))

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=40))

    assert "first" in result.stdout and "second" in result.stdout
    first_at = next(at for text, at in seen if "first" in text)
    assert first_at < 1.0, (
        f"'first' surfaced at {first_at:.1f}s — it was buffered until the "
        f"command ended rather than streamed")


def test_a_renderer_that_throws_does_not_kill_the_command():
    """The reader is also what keeps the pipe from filling. A callback that
    raises must cost its own output and nothing else — if it took the reader
    down, a bad renderer would hang the command it was displaying."""
    calls = []

    def _boom(_stream, _text):
        calls.append(1)
        raise RuntimeError("renderer is broken")

    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()), CellPolicy())
        await cell.start()
        return await cell.run("echo one; echo two; echo three", timeout=20,
                              on_output=_boom)

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert result.exit_code == 0
    assert "three" in result.stdout, "the command's own result was lost"
    assert len(calls) == 1, "a faulting renderer was called again and again"


# ── not being killed ─────────────────────────────────────────────────────────


def test_the_interactive_posture_does_not_kill_a_long_command():
    """`kill_on_timeout=False` is the operator saying "I decide". A command past
    its budget keeps running and is reported on, rather than stopped."""
    notices: list[str] = []

    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()),
                              CellPolicy(default_timeout_s=1, kill_on_timeout=False))
        await cell.start()
        return await cell.run("sleep 3; echo survived", timeout=1,
                              on_output=lambda s, t: notices.append(t))

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=40))

    assert result.timed_out is False
    assert result.exit_code == 0
    assert "survived" in result.stdout, "the command was killed at its budget"
    assert any("still running" in n for n in notices), (
        "the operator was never told the command had overrun")


def test_the_headless_posture_still_kills():
    """Nobody is watching a dispatched job, so there is nobody to decide. The
    default has to remain the one that cannot hang."""
    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()),
                              CellPolicy(default_timeout_s=1))
        await cell.start()
        return await cell.run("sleep 30", timeout=1)

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert result.timed_out is True
    assert result.exit_code == 124


# ── being able to stop it ────────────────────────────────────────────────────


def test_an_interrupt_reaches_a_command_already_running():
    """The engine checks its abort signal BETWEEN tool batches, so before this
    an interrupt during a ten-minute command was noticed ten minutes later. With
    nothing killing long commands any more, that gap stops being a delay and
    becomes a hang."""
    from forge.warden.dispatch import dispatch_tool
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.tool import ToolContext
    from forge.tools.shell import RunCommand

    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()),
                              CellPolicy(default_timeout_s=60, kill_on_timeout=False))
        await cell.start()
        ctx = ToolContext(agent_id="t", cell=cell, graph=None,
                          files=FileStateCache(), permissions=PermissionEngine(),
                          network_allowed=False)
        abort = asyncio.Event()

        async def _press_ctrl_c():
            await asyncio.sleep(0.5)
            abort.set()

        asyncio.ensure_future(_press_ctrl_c())
        began = time.monotonic()
        res = await dispatch_tool({"run_command": RunCommand()}, "run_command",
                                  {"command": "sleep 60"}, ctx, abort=abort)
        return res, time.monotonic() - began

    res, elapsed = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert elapsed < 10, f"the interrupt took {elapsed:.1f}s to land"
    assert res.is_error
    assert "INTERRUPTED" in res.content
    assert "person's decision" in res.content


def test_stopping_a_command_actually_kills_it():
    """"Stop" must mean the process dies, not merely that nothing is waiting on
    it any more. The no-kill path shields its readers so a passed deadline
    cannot cancel them, and that shield would happily survive the operator's
    cancellation too — leaving the command running, holding its port and its
    lock, with nobody watching."""
    from forge.warden.dispatch import dispatch_tool
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.tool import ToolContext
    from forge.tools.shell import RunCommand

    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        cell = SubprocessCell(tmp, CellPolicy(default_timeout_s=60,
                                              kill_on_timeout=False))
        await cell.start()
        ctx = ToolContext(agent_id="t", cell=cell, graph=None,
                          files=FileStateCache(), permissions=PermissionEngine(),
                          network_allowed=False)
        abort = asyncio.Event()
        marker = (tmp / "ticks").as_posix()

        async def _press_ctrl_c():
            await asyncio.sleep(0.6)
            abort.set()

        asyncio.ensure_future(_press_ctrl_c())
        await dispatch_tool(
            {"run_command": RunCommand()}, "run_command",
            {"command": f"for i in $(seq 1 60); do echo . >> {marker}; sleep 0.2; done"},
            ctx, abort=abort)

        path = tmp / "ticks"
        at_stop = path.read_text().count(".") if path.exists() else 0
        await asyncio.sleep(1.5)
        later = path.read_text().count(".") if path.exists() else 0
        return at_stop, later

    at_stop, later = asyncio.run(asyncio.wait_for(scenario(), timeout=40))

    assert later == at_stop, (
        f"the command wrote {later - at_stop} more times after being stopped — "
        f"the interrupt returned but the process kept running")


def test_an_interrupt_is_not_reported_as_a_timeout():
    """They ask the model for opposite things. A deadline says "this hung, route
    around it"; an interrupt says "a person stopped you, report and stand
    down". Conflating them turns a decision into an obstacle to be worked
    around, which is the one response that must not follow ctrl+c."""
    from forge.warden.dispatch import dispatch_tool
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.tool import ToolContext
    from forge.tools.shell import RunCommand

    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()),
                              CellPolicy(default_timeout_s=60, kill_on_timeout=False))
        await cell.start()
        ctx = ToolContext(agent_id="t", cell=cell, graph=None,
                          files=FileStateCache(), permissions=PermissionEngine(),
                          network_allowed=False)
        abort = asyncio.Event()
        abort.set()
        return await dispatch_tool({"run_command": RunCommand()}, "run_command",
                                   {"command": "sleep 60"}, ctx, abort=abort)

    res = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert "TOOL_TIMEOUT" not in res.content
    assert "Do not retry" in res.content


# ── the buffer underneath all of it ──────────────────────────────────────────


def test_retained_keeps_both_ends_and_counts_the_middle():
    buf = Retained(1_000)
    buf.feed(b"HEAD" + b"x" * 50_000 + b"TAIL")
    text = buf.text()

    assert text.startswith("HEAD")
    assert text.endswith("TAIL")
    assert "omitted from the middle" in text
    assert len(text) < 1_400
