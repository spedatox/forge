"""Noticing a file move underneath the model.

Forge tracked read-before-write from the start, which catches the stale edit at
the moment it is attempted. What it said nothing about is the gap before that:
the model runs a formatter, and everything it believes about six files is now
wrong — including the parts it will reason from without editing anything.

The two halves are tested separately because they fail separately. The cache
half must announce a change without forgiving it; the loop half must find the
change without paying for the search on turns that could not have caused one.
"""
from __future__ import annotations

import asyncio

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache, digest
from forge.warden.permissions import PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult
from tests.test_forge import Echo

# ── the cache: announce it, but do not forgive it ────────────────────────────


def test_a_file_rewritten_underneath_is_reported_once():
    files = FileStateCache()
    files.record("a.py", "one", digest("one"))

    assert files.note_external_change("a.py", digest("two")) is True
    assert files.note_external_change("a.py", digest("two")) is False


def test_a_second_change_is_reported_again():
    """The once-per-job restraint that governs the reminder RULES must not
    apply here. Two formatter runs are two facts, and the second one is
    precisely when an edit lands on text nobody has looked at."""
    files = FileStateCache()
    files.record("a.py", "one", digest("one"))
    files.note_external_change("a.py", digest("two"))

    assert files.note_external_change("a.py", digest("three")) is True


def test_an_unchanged_file_says_nothing():
    files = FileStateCache()
    files.record("a.py", "one", digest("one"))
    assert files.note_external_change("a.py", digest("one")) is False


def test_an_untracked_file_says_nothing():
    """A file the model never read cannot have gone stale for it."""
    assert FileStateCache().note_external_change("a.py", digest("x")) is False


def test_announcing_a_change_does_not_forgive_the_edit():
    """The bug this shape exists to avoid. If the sweep updated the recorded
    digest, read-before-write would go quiet at exactly the moment it matters
    and a blind edit against remembered text would sail through."""
    files = FileStateCache()
    files.record("a.py", "one", digest("one"))
    files.note_external_change("a.py", digest("two"))

    assert files.freshness_error("a.py", digest("two")) is not None


def test_re_reading_clears_the_pending_announcement():
    """The model looked. There is nothing left to tell it."""
    files = FileStateCache()
    files.record("a.py", "one", digest("one"))
    files.note_external_change("a.py", digest("two"))
    files.record("a.py", "two", digest("two"))

    assert files.freshness_error("a.py", digest("two")) is None
    assert files.note_external_change("a.py", digest("two")) is False


def test_the_sweep_list_is_bounded_and_newest_first():
    """One filesystem read per entry. A file read forty turns ago is not what
    the model is about to edit."""
    files = FileStateCache()
    for i in range(30):
        files.record(f"f{i}.py", "x", digest("x"))

    assert files.tracked(limit=3) == ["f29.py", "f28.py", "f27.py"]


def test_sweeping_does_not_reorder_what_the_next_sweep_will_look_at():
    """A sweep is the harness checking up on everything, not the model using a
    file. If it promoted each entry it touched it would rewrite recency into
    sweep order — and since the sweep walks newest-first, that order is
    inverted, so the next twenty would be the wrong twenty."""
    files = FileStateCache()
    for i in range(5):
        files.record(f"f{i}.py", "x", digest("x"))
    before = files.tracked()

    for path in files.tracked():
        files.note_external_change(path, digest("changed"))

    assert files.tracked() == before


# ── the loop: find it, and do not pay for it when nothing could have moved ───


class _Cell:
    """A workspace something else is writing to."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.reads: list[str] = []

    async def read(self, path: str) -> str:
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write(self, path: str, content: str) -> None:
        self.files[path] = content


class Mutating(Tool):
    """Stands in for `run_command`: it declares that it writes, which is the
    only thing the sweep's trigger reads."""
    name = "shell"
    description = "x" * 40
    Args = Echo.Args
    READ_ONLY = False

    async def call(self, args, ctx) -> ToolResult:
        return ToolResult("ran")


def _warden(steps, cell, files):
    ctx = ToolContext(agent_id="t", cell=cell, graph=None, files=files,
                      permissions=PermissionEngine(), network_allowed=False)
    return Warden(system_prompt="", tools={"echo": Echo(), "shell": Mutating()},
                  model=ScriptedModel(steps), ctx=ctx, max_iterations=8)


def test_a_file_changed_by_a_command_is_announced_to_the_model():
    cell = _Cell({"a.py": "formatted"})
    files = FileStateCache()
    files.record("a.py", "original", digest("original"))

    steps = [lambda m: ("running", [tool_call("shell", text="ruff format")]),
             lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, cell, files).run("tidy up"))

    transcript = str(term.messages)
    assert "changed on disk since you read" in transcript
    assert "a.py" in transcript


def test_a_read_only_turn_does_not_touch_the_filesystem():
    """The sweep costs one read per tracked file. On a long exploration most
    turns cannot have changed anything, and those turns must cost nothing."""
    cell = _Cell({"a.py": "formatted"})
    files = FileStateCache()
    files.record("a.py", "original", digest("original"))

    steps = [lambda m: ("looking", [tool_call("echo", text="hello")]),
             lambda m: ("done", [])]
    asyncio.run(_warden(steps, cell, files).run("look around"))

    assert cell.reads == []


def test_a_file_that_vanished_is_not_this_rules_business():
    """Read-before-write will say so at the edit, where it is actionable. A
    sweep that crashed on a deleted temp file would take the job with it."""
    cell = _Cell({})
    files = FileStateCache()
    files.record("gone.py", "original", digest("original"))

    steps = [lambda m: ("running", [tool_call("shell", text="rm gone.py")]),
             lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, cell, files).run("clean"))

    assert term.reason is StopReason.COMPLETED
    assert "changed on disk" not in str(term.messages)
