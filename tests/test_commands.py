"""REPL commands.

The git-facing ones run through the **Cell**, not the host, and that is the
whole point of them: inside a worktree the agent's working directory is not the
operator's, so a `git diff` typed in another terminal would show nothing while
the agent has changed a dozen files. Asking the Cell asks where the work
actually happened.

`/doctor` is here because every one of its checks answers a question that
otherwise surfaces mid-turn as a confusing failure — a missing key looks like a
broken agent.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forge.tui.commands import REGISTRY, command_help, resolve


class _Result:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code
        self.timed_out = False


class _Cell:
    """Records what was asked of it and answers from a script."""

    def __init__(self, answers: dict[str, _Result] | None = None, subpath=""):
        self.answers = answers or {}
        self.subpath = subpath
        self.ran: list[str] = []

    async def run(self, command, timeout=None, env=None):
        self.ran.append(command)
        for prefix, result in self.answers.items():
            if command.startswith(prefix):
                return result
        return _Result()


class _Cfg:
    agent_id = "optimus"
    permission_mode = "act"


class _Session:
    def __init__(self, tmp_path, cell=None, messages=None):
        self.cfg = _Cfg()
        self.model_ref = "deepseek:deepseek-v4-pro"
        self.workspace = tmp_path
        self.cell = cell
        self.messages = messages or []
        self.turns = 0
        self.tools = {"read_file": object()}

    @property
    def permission_mode(self):
        return "act"


def _run(name: str, args: str, session):
    cmd, _ = resolve(f"/{name}")
    return asyncio.run(cmd.run(args, session))


# ── Registration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "diff", "status", "branch", "doctor", "keybindings", "export",
])
def test_the_command_is_registered(name):
    assert name in REGISTRY


def test_aliases_resolve_to_the_same_command():
    assert resolve("/st")[0] is resolve("/status")[0]
    assert resolve("/keys")[0] is resolve("/keybindings")[0]


def test_help_covers_every_command():
    assert set(command_help()) >= {"diff", "status", "doctor", "export"}


# ── git commands go through the Cell ────────────────────────────────────────


def test_diff_asks_the_cell_not_the_host(tmp_path):
    """Inside a worktree the operator's own terminal is in the wrong directory."""
    cell = _Cell({"git diff --stat": _Result(" calc.py | 2 +-\n"),
                  "git diff": _Result("@@ -1 +1 @@\n-a\n+b\n")})
    out = _run("diff", "", _Session(tmp_path, cell))

    assert any(c.startswith("git diff") for c in cell.ran)
    assert "calc.py" in out.text


def test_a_clean_tree_says_so(tmp_path):
    out = _run("diff", "", _Session(tmp_path, _Cell()))
    assert "clean" in out.text.lower()


def test_status_reports_the_active_worktree(tmp_path):
    """The one piece of state that silently changes where every edit lands."""
    cell = _Cell(subpath=".forge-worktrees/fix")
    out = _run("status", "", _Session(tmp_path, cell))

    assert ".forge-worktrees/fix" in out.text
    assert "optimus" in out.text


def test_status_survives_a_non_repository(tmp_path):
    cell = _Cell({"git status": _Result(stderr="not a git repository", exit_code=128)})
    out = _run("status", "", _Session(tmp_path, cell))
    assert "not a git repository" in out.text


def test_branch_with_no_argument_reports_it(tmp_path):
    cell = _Cell({"git branch --show-current": _Result("main\n")})
    assert "main" in _run("branch", "", _Session(tmp_path, cell)).text


def test_branch_with_an_argument_checks_it_out(tmp_path):
    cell = _Cell({"git checkout": _Result("Switched to branch 'x'\n")})
    _run("branch", "x", _Session(tmp_path, cell))
    assert any("git checkout x" in c for c in cell.ran)


def test_a_git_command_without_a_cell_does_not_raise(tmp_path):
    assert _run("diff", "", _Session(tmp_path, cell=None)).text


def test_a_cell_that_throws_does_not_end_the_session(tmp_path):
    class _Broken(_Cell):
        async def run(self, command, timeout=None, env=None):
            raise RuntimeError("docker is not running")

    out = _run("status", "", _Session(tmp_path, _Broken()))
    assert "docker is not running" in out.text


# ── /doctor ─────────────────────────────────────────────────────────────────


def test_doctor_flags_the_missing_key_for_the_configured_provider(tmp_path, monkeypatch):
    """A missing key looks like a broken agent until something says otherwise."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    out = _run("doctor", "", _Session(tmp_path, _Cell()))

    assert "DEEPSEEK_API_KEY" in out.text
    assert "MISS" in out.text


def test_doctor_passes_when_the_key_is_present(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    out = _run("doctor", "", _Session(tmp_path, _Cell()))
    line = [ln for ln in out.text.splitlines() if "DEEPSEEK_API_KEY" in ln][0]
    assert "ok" in line


def test_doctor_names_the_consequence_of_each_gap(tmp_path, monkeypatch):
    """A check that only says MISS makes the operator guess what it cost them."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = _run("doctor", "", _Session(tmp_path, _Cell()))
    assert "web_search will refuse" in out.text


def test_doctor_notices_a_missing_cell(tmp_path):
    out = _run("doctor", "", _Session(tmp_path, cell=None))
    assert "no sandbox" in out.text


# ── /export ─────────────────────────────────────────────────────────────────


def test_export_writes_the_transcript(tmp_path):
    session = _Session(tmp_path, _Cell(), messages=[{"role": "user", "content": "hi"}])
    out = _run("export", "out.json", session)

    assert (tmp_path / "out.json").exists()
    assert "1 messages" in out.text


def test_export_of_an_empty_session_says_so(tmp_path):
    assert "Nothing to export" in _run("export", "", _Session(tmp_path, _Cell())).text


def test_export_names_the_file_when_none_is_given(tmp_path):
    session = _Session(tmp_path, _Cell(), messages=[{"role": "user", "content": "hi"}])
    _run("export", "", session)
    assert list(tmp_path.glob("forge-*.json"))


# ── /keybindings ────────────────────────────────────────────────────────────


def test_keybindings_documents_the_undiscoverable_ones(tmp_path):
    text = _run("keybindings", "", _Session(tmp_path, _Cell())).text
    for key in ("ctrl+r", "shift+tab", "ctrl+o", "!cmd", "@path"):
        assert key in text
