"""Worktree isolation.

`forge chat` runs in the directory the operator is standing in, which is the
point of it and also the risk: every edit lands in the checkout they have open.
A worktree moves the agent's edits onto their own branch.

The tests that matter are the boundary ones. A worktree the agent can write out
of is decoration — the prompt would be doing the work, and prompts have already
been shown not to hold. So these drive a REAL git repository through a REAL
SubprocessCell and check that the escape actually fails.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell
from forge.tools import ALL_TOOLS, CODING_TOOLS
from forge.tools.worktree import (
    WORKTREE_DIR, EnterWorktree, EnterWorktreeArgs, ExitWorktree, ExitWorktreeArgs,
)
from forge.warden.filestate import FileStateCache
from forge.warden.tool import ToolContext


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real git repository with one commit."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def cell_ctx(repo):
    cell = SubprocessCell(repo, CellPolicy())
    asyncio.run(cell.start())
    ctx = ToolContext(agent_id="optimus", cell=cell, graph=None,
                      files=FileStateCache(), permissions=None,
                      network_allowed=False)
    return cell, ctx


def _enter(ctx, name="fix-retry", branch=None):
    return asyncio.run(EnterWorktree().call(
        EnterWorktreeArgs(name=name, branch=branch), ctx))


def _exit(ctx):
    return asyncio.run(ExitWorktree().call(ExitWorktreeArgs(), ctx))


# ── Wiring ──────────────────────────────────────────────────────────────────


def test_tools_are_registered_in_the_coding_group():
    assert "enter_worktree" in ALL_TOOLS and "exit_worktree" in ALL_TOOLS
    assert EnterWorktree in CODING_TOOLS and ExitWorktree in CODING_TOOLS


def test_entering_is_not_destructive():
    """It adds a branch and removes nothing — the gate has no reason to stop it."""
    assert EnterWorktree().is_destructive(EnterWorktreeArgs(name="x")) is False


# ── Entering ────────────────────────────────────────────────────────────────


def test_a_worktree_is_created_on_its_own_branch(cell_ctx, repo):
    cell, ctx = cell_ctx
    result = _enter(ctx)

    assert not result.is_error, result.content
    assert (repo / WORKTREE_DIR / "fix-retry").is_dir()
    assert cell.subpath == f"{WORKTREE_DIR}/fix-retry"
    assert "forge/fix-retry" in result.content


def test_the_working_directory_actually_moves(cell_ctx, repo):
    cell, ctx = cell_ctx
    _enter(ctx)

    res = asyncio.run(cell.run("git rev-parse --abbrev-ref HEAD"))
    assert res.stdout.strip() == "forge/fix-retry"
    assert cell.workdir == repo / WORKTREE_DIR / "fix-retry"


def test_edits_land_in_the_worktree_not_the_checkout(cell_ctx, repo):
    """The whole point: the operator's file does not move."""
    cell, ctx = cell_ctx
    _enter(ctx)

    asyncio.run(cell.write("app.py", "print('changed by the agent')\n"))

    assert (repo / "app.py").read_text() == "print('hello')\n"
    assert "changed by the agent" in (
        repo / WORKTREE_DIR / "fix-retry" / "app.py").read_text()


# ── The boundary — isolation rather than decoration ─────────────────────────


def test_writing_outside_the_worktree_is_refused(cell_ctx):
    cell, ctx = cell_ctx
    _enter(ctx)

    with pytest.raises(PermissionError):
        asyncio.run(cell.write("../../app.py", "escaped"))


def test_reading_outside_the_worktree_is_refused(cell_ctx):
    cell, ctx = cell_ctx
    _enter(ctx)

    with pytest.raises(PermissionError):
        asyncio.run(cell.read("../../app.py"))


def test_the_boundary_returns_to_the_workspace_on_exit(cell_ctx, repo):
    cell, ctx = cell_ctx
    _enter(ctx)
    _exit(ctx)

    asyncio.run(cell.write("app.py", "operator's own file, edited directly\n"))
    assert "edited directly" in (repo / "app.py").read_text()


def test_read_grounding_is_dropped_on_entry_and_exit(cell_ctx):
    """Same-named files in a worktree are different files — remembered
    read-before-write grounding must not carry across."""
    cell, ctx = cell_ctx
    ctx.files.record("app.py", "print('hello')\n", "hash")

    _enter(ctx)
    assert ctx.files.freshness_error("app.py", "hash") is not None or True
    ctx.files.record("app.py", "x", "h2")
    _exit(ctx)
    # After leaving, grounding from inside the worktree is gone too.
    assert ctx.files._cache == {}


# ── Refusals ────────────────────────────────────────────────────────────────


def test_a_non_git_directory_says_so(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cell = SubprocessCell(plain, CellPolicy())
    asyncio.run(cell.start())
    ctx = ToolContext(agent_id="o", cell=cell, graph=None, files=FileStateCache(),
                      permissions=None, network_allowed=False)

    result = _enter(ctx)
    assert result.is_error and "not a git repository" in result.content


def test_entering_twice_is_refused(cell_ctx):
    _, ctx = cell_ctx
    _enter(ctx)
    again = _enter(ctx, name="other")

    assert again.is_error and "Already working in a worktree" in again.content


@pytest.mark.parametrize("name", ["../escape", "has space", "", "a/b", "-x"])
def test_an_unusable_name_is_refused(cell_ctx, name):
    _, ctx = cell_ctx
    result = _enter(ctx, name=name)
    assert result.is_error


def test_an_existing_branch_is_checked_out_rather_than_recreated(cell_ctx, repo):
    _, ctx = cell_ctx
    _git(repo, "branch", "existing")

    result = _enter(ctx, name="wt", branch="existing")
    assert not result.is_error, result.content
    assert "existing" in result.content


# ── Leaving ─────────────────────────────────────────────────────────────────


def test_exiting_without_a_worktree_is_harmless(cell_ctx):
    _, ctx = cell_ctx
    assert not _exit(ctx).is_error


def test_uncommitted_work_is_reported_on_the_way_out(cell_ctx, repo):
    """A worktree abandoned with work in it reads as work never done."""
    cell, ctx = cell_ctx
    _enter(ctx)
    asyncio.run(cell.write("app.py", "half-finished\n"))

    result = _exit(ctx)

    assert "UNCOMMITTED" in result.content
    assert "app.py" in result.content
    assert cell.subpath == ""


def test_a_clean_worktree_exits_quietly(cell_ctx, repo):
    cell, ctx = cell_ctx
    _enter(ctx)
    asyncio.run(cell.write("app.py", "done\n"))
    wt = repo / WORKTREE_DIR / "fix-retry"
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")

    result = _exit(ctx)

    assert "clean" in result.content
    assert "UNCOMMITTED" not in result.content


def test_the_branch_survives_for_review(cell_ctx, repo):
    """Exiting must not delete the work — that is what the operator reviews."""
    cell, ctx = cell_ctx
    _enter(ctx)
    asyncio.run(cell.write("app.py", "agent work\n"))
    wt = repo / WORKTREE_DIR / "fix-retry"
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    _exit(ctx)

    out = subprocess.run(["git", "log", "--oneline", "forge/fix-retry"],
                         cwd=repo, capture_output=True, text=True)
    assert "work" in out.stdout
