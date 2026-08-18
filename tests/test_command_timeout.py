"""A command must not outlive its own deadline, and neither must the call.

Reported as "Forge sometimes gets stuck running a command". Two faults sat
behind it, and the second is the one that actually hangs.

`run` killed the SHELL on timeout and nothing else. Everything an agent really
runs — pytest, npm, a dev server, a build — is a child of that shell, so the
children survived: still holding the port, still holding the lock, still
holding the stdout pipe they inherited.

That last one is what turns a leak into a hang. `proc.communicate()` is
cancelled by the deadline, the shell is killed, and then `await proc.wait()`
blocks until the pipe closes — which it cannot, because a live grandchild is
still holding the write end. On a two-second timeout with a short-lived child
the call took seven seconds to come back. With a dev server it does not come
back at all: `run_command` never returns, the live region sits on
`Running run_command` for as long as anyone lets it, and ctrl+c cannot help
because the turn is parked inside that one tool dispatch.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None and __import__("os").name == "nt",
    reason="needs a POSIX shell to spawn a background grandchild",
)


def _cell(tmp: Path) -> SubprocessCell:
    return SubprocessCell(tmp, CellPolicy(default_timeout_s=2))


# A shell that waits on a child which keeps the inherited stdout pipe open long
# past the deadline. This is `npm start`, a watcher, a dev server, a build that
# backgrounds its own worker — the shape, not a contrivance.
_OUTLIVES_ITS_SHELL = "(sleep 30) & wait"


def test_the_call_returns_within_its_timeout():
    """The hang, at its narrowest. Nothing else in this file matters if the
    call itself does not come back.

    Killing the shell is not enough to end the call: `communicate()` has
    already been cancelled by the deadline, and the `wait()` that follows is
    held up by a descendant still holding the write end of the pipe. Measured
    at 7s on a 2s deadline with a child that eventually exits; with one that
    does not, it never returns at all."""
    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        cell = _cell(tmp)
        await cell.start()
        started = time.monotonic()
        result = await cell.run(_OUTLIVES_ITS_SHELL, timeout=2)
        return time.monotonic() - started, result

    elapsed, result = asyncio.run(asyncio.wait_for(scenario(), timeout=40))

    assert elapsed < 5, (
        f"took {elapsed:.1f}s on a 2s deadline — the call is waiting on a pipe "
        f"a surviving descendant is still holding")
    assert result.exit_code in (0, 124)


def test_a_timed_out_command_takes_its_children_with_it():
    """A killed shell whose children live on hands the next command a workspace
    that is still busy — the port bound, the lock held — and Forge looks stuck
    running something it has not started yet."""
    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        cell = _cell(tmp)
        await cell.start()
        marker = (tmp / "alive").as_posix()
        # The shell returns at once; the grandchild ticks for far longer than
        # the deadline. If it survives the kill, the file keeps growing.
        await cell.run(
            f"(for i in $(seq 1 60); do echo . >> {marker}; sleep 0.2; done) & wait",
            timeout=2)
        path = tmp / "alive"
        at_kill = path.read_text().count(".") if path.exists() else 0
        await asyncio.sleep(2)
        later = path.read_text().count(".") if path.exists() else 0
        return at_kill, later

    at_kill, later = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert later == at_kill, (
        f"the grandchild wrote {later - at_kill} more times after its command "
        f"was killed — only the shell died")


def test_a_command_that_finishes_is_untouched():
    """The guard is for the deadline. Ordinary commands must not notice it."""
    async def scenario():
        cell = _cell(Path(tempfile.mkdtemp()))
        await cell.start()
        return await cell.run("echo hello", timeout=10)

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=20))

    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.timed_out is False


def test_a_timed_out_command_still_reports_what_it_printed():
    """A timeout that discards the output is a diagnosable hang turned into a
    guess. The output before the wedge is the whole diagnosis: a hanging pytest
    names the test it stopped on, a hanging build names the file.

    DockerCell never lost it — `timeout -s KILL` fires inside the container, so
    `docker exec` returns normally with the output — so the two backends
    disagreed about what a timeout tells you, which is the kind of difference
    that gets debugged twice."""
    async def scenario():
        cell = _cell(Path(tempfile.mkdtemp()))
        await cell.start()
        return await cell.run("echo before-the-hang; sleep 30", timeout=2)

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert result.timed_out is True
    assert result.exit_code == 124
    assert "before-the-hang" in result.stdout, "the output before the wedge was dropped"
    assert "timed out" in result.stderr


def test_a_runaway_writer_cannot_grow_the_buffer_without_bound():
    """Draining into a buffer reintroduces the growth `max_output_bytes` exists
    to stop — and worse than before, because it now accumulates for as long as
    the timeout allows rather than being capped by a finished result."""
    async def scenario():
        cell = SubprocessCell(Path(tempfile.mkdtemp()),
                              CellPolicy(default_timeout_s=1, max_output_bytes=4_000))
        await cell.start()
        # A shell builtin loop rather than `yes`, so the test does not depend on
        # which coreutils the platform's bash shipped with.
        return await cell.run("while :; do echo abcdefghijklmnop; done", timeout=1)

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert len(result.stdout) < 8_000, "the retained buffer is not bounded"
    assert "omitted from the middle" in result.stdout


def test_a_killed_command_tells_the_model_the_limit_is_raisable():
    """The reported symptom: "I keep hitting the 300s timeout and the turn ends
    like it gets confused".

    Centurion's profile sets the Cell to 300s, so a full suite or a wide scan is
    killed mid-run. The model was handed `exit_code: 124` and one line saying so
    — nothing about what the limit was, and nothing about the `timeout` argument
    that would have raised it to the ceiling. With no lever visible, the only
    moves left are re-run the identical command into the identical wall, or stop
    and report the work blocked. Both look like confusion from outside; neither
    is."""
    from forge.tools.shell import RunCommand, RunCommandArgs
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.tool import ToolContext

    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        cell = SubprocessCell(tmp, CellPolicy(default_timeout_s=1))
        await cell.start()
        ctx = ToolContext(agent_id="t", cell=cell, graph=None,
                          files=FileStateCache(), permissions=PermissionEngine(),
                          network_allowed=False)
        return await RunCommand().call(
            RunCommandArgs(command="echo starting; sleep 30", timeout=1), ctx)

    res = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert res.is_error
    assert "starting" in res.content, "the output before the wall was dropped"
    assert "The limit was 1s" in res.content, "the model was not told the budget"
    assert "pass `timeout` up to 600" in res.content, "the lever was not named"


def test_at_the_ceiling_the_advice_stops_offering_more_time():
    """Advice that says "ask for longer" when longer is not available sends the
    model round the same loop with a bigger number. At the ceiling the honest
    answer is to split the work."""
    from forge.tools.shell import RunCommand, RunCommandArgs
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.tool import ToolContext

    async def scenario():
        tmp = Path(tempfile.mkdtemp())
        cell = SubprocessCell(tmp, CellPolicy(default_timeout_s=1, max_timeout_s=1))
        await cell.start()
        ctx = ToolContext(agent_id="t", cell=cell, graph=None,
                          files=FileStateCache(), permissions=PermissionEngine(),
                          network_allowed=False)
        return await RunCommand().call(
            RunCommandArgs(command="sleep 30", timeout=1), ctx)

    res = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert "a longer timeout is not available" in res.content.replace("\n", " ")
    assert "Split the work" in res.content


def test_the_command_runs_in_a_group_of_its_own():
    """The kill needs a handle that survives the shell exiting first, and a
    process group is the only one there is. Set at launch because by the time
    the deadline fires there may be no parent left to look it up from."""
    import inspect

    from forge.cell import subprocess_cell

    src = inspect.getsource(subprocess_cell.SubprocessCell.run)
    assert src.count("_own_process_group()") == 2, "both launch paths, not one"
