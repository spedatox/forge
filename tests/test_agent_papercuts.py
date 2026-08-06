"""Three faults that cost the agent a turn each, every time.

None of them is a crash. Each one just makes an ordinary, correct instinct fail
— so the model spends a call discovering the harness disagrees with it, then
works around it. Observed in one real session:

    Run(mkdir -p .forge-worktrees/responsive/public)
      -> The syntax of the command is incorrect.        # cmd.exe, not a shell
    Review:grep(.forge-worktrees/responsive/hisar.jsx)
      -> Not a directory in the workspace               # a file is a fine target
    GraphQuery(...) / GraphOverview(...)
      -> The codebase graph is unavailable              # twice, then gave up
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell, _posix_shell
from forge.tools.search import Glob, GlobArgs, Grep, GrepArgs
from forge.warden.toolsource import GRAPH_TOOLS, without_graph_tools


# ── One shell dialect, everywhere ────────────────────────────────────────────


def _cell(tmp_path) -> SubprocessCell:
    cell = SubprocessCell(tmp_path, CellPolicy())
    asyncio.run(cell.start())
    return cell


def test_posix_commands_work_on_the_operators_machine(tmp_path):
    """`mkdir -p` is what anyone writes. Under cmd.exe it fails with a message
    that does not even say which dialect it wanted."""
    cell = _cell(tmp_path)
    result = asyncio.run(cell.run("mkdir -p a/b/c && echo made"))

    assert result.exit_code == 0, result.stderr
    assert "made" in result.stdout
    assert (tmp_path / "a" / "b" / "c").is_dir()


def test_pipes_and_quoting_survive(tmp_path):
    """The other half: cmd would mangle the quoting on the way through."""
    cell = _cell(tmp_path)
    result = asyncio.run(cell.run("echo 'hello   world' | tr -s ' '"))

    assert result.exit_code == 0
    assert "hello world" in result.stdout


def test_the_dialect_is_reported_not_guessed(tmp_path):
    """An agent that has to infer its shell from a failed command has already
    spent the turn."""
    assert _cell(tmp_path).shell_dialect in ("posix", "cmd")


@pytest.mark.skipif(os.name != "nt", reason="the fallback only exists on Windows")
def test_windows_uses_a_real_shell_when_one_exists():
    """Git ships bash on essentially every Windows dev machine, so one dialect
    works on the laptop and the server alike."""
    assert _posix_shell(), "no POSIX shell found — commands will run under cmd.exe"


@pytest.mark.skipif(os.name == "nt", reason="POSIX already has a shell")
def test_posix_does_not_reach_for_a_wrapper():
    assert _posix_shell() is None


def test_the_workspace_is_still_the_boundary(tmp_path):
    """Changing how commands are launched must not change where they run."""
    cell = _cell(tmp_path)
    result = asyncio.run(cell.run("pwd"))
    assert result.exit_code == 0
    assert Path(result.stdout.strip()).name == tmp_path.name


# ── grep on one file ─────────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, root):
        class _Cell:
            host_path = root
        self.cell = _Cell()
        self.agent_id = "t"


def test_grep_searches_a_single_file(tmp_path):
    """"Search inside this one file" is an ordinary request. Refusing it sent
    the caller off to read the whole file instead — the exact cost grep exists
    to avoid."""
    (tmp_path / "hisar.jsx").write_text("const showSidebar = true\n", encoding="utf-8")

    result = asyncio.run(Grep().call(
        GrepArgs(pattern="showSidebar", path="hisar.jsx"), _Ctx(tmp_path)))

    assert not result.is_error, result.content
    assert "showSidebar" in result.content


def test_grep_still_walks_a_directory(tmp_path):
    (tmp_path / "a.py").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing\n", encoding="utf-8")

    result = asyncio.run(Grep().call(GrepArgs(pattern="needle", path="."), _Ctx(tmp_path)))

    assert not result.is_error
    assert "a.py" in result.content


def test_grep_on_a_missing_path_says_so(tmp_path):
    result = asyncio.run(Grep().call(
        GrepArgs(pattern="x", path="nope.txt"), _Ctx(tmp_path)))
    assert result.is_error and "No such path" in result.content


def test_glob_on_a_file_explains_the_category_error(tmp_path):
    """Unlike grep, globbing one file is not a narrower search — it is the
    wrong tool, and 'not a directory' reads as though the path were wrong."""
    (tmp_path / "x.jsx").write_text("hi", encoding="utf-8")

    result = asyncio.run(Glob().call(
        GlobArgs(pattern="*.js", path="x.jsx"), _Ctx(tmp_path)))

    assert result.is_error
    assert "is a file" in result.content
    assert "grep" in result.content        # points at the tool that would work


# ── a tool that cannot work is not offered ───────────────────────────────────


def test_graph_tools_are_withheld_when_there_is_no_graph():
    """Their own descriptions say to reach for them FIRST to orient yourself.
    Offering them with no sidecar running spends a call — often two, since
    'unavailable' reads as 'not indexed yet' rather than 'not available here'."""
    tools = {name: object() for name in
             ("read_file", "grep", "graph_query", "graph_path", "graph_overview")}

    remaining = without_graph_tools(tools)

    assert set(remaining) == {"read_file", "grep"}


def test_the_rest_of_the_toolset_is_untouched():
    tools = {name: object() for name in ("read_file", "write_file", "run_command")}
    assert without_graph_tools(tools) == tools


def test_every_graph_tool_is_covered():
    """A fourth graph tool added later must not silently keep being offered."""
    from forge.tools import ALL_TOOLS
    assert {n for n in ALL_TOOLS if n.startswith("graph_")} == set(GRAPH_TOOLS)
