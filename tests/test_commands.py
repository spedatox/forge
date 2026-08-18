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

    async def run(self, command, timeout=None, env=None, on_output=None):
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
        async def run(self, command, timeout=None, env=None, on_output=None):
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


# ── /vim, /copy, /mcp, /permissions ─────────────────────────────────────────


class _Bar:
    """Stands in for the input line. `_session` present means a real editor."""

    def __init__(self, has_editor=True, vi=False):
        self._session = object() if has_editor else None
        self._vi = vi

    @property
    def vi_mode(self):
        return self._vi

    def set_vi_mode(self, on):
        self._vi = bool(on)
        return self._vi


class _AllowList:
    def __init__(self, entries=(), path=None):
        self.entries = set(entries)
        self.path = path


def _session_with(tmp_path, **kw):
    s = _Session(tmp_path, _Cell())
    s.input_bar = kw.pop("bar", None)
    s.allowlist = kw.pop("allowlist", _AllowList())
    s.tools = kw.pop("tools", {})
    s.messages = kw.pop("messages", [])
    return s


def test_vim_toggles_and_reports_the_mode(tmp_path):
    session = _session_with(tmp_path, bar=_Bar())

    assert "vi" in _run("vim", "", session).text
    assert "emacs" in _run("vim", "", session).text
    assert "vi" in _run("vim", "on", session).text
    assert "emacs" in _run("vim", "off", session).text


def test_vim_without_an_editor_does_not_send_you_to_reinstall(tmp_path):
    """prompt_toolkit may be installed and still unable to drive the terminal —
    telling the operator to install it sends them to fix the wrong thing."""
    import forge.tui.input as input_mod

    session = _session_with(tmp_path, bar=_Bar(has_editor=False))
    text = _run("vim", "", session).text

    if input_mod.AVAILABLE:
        assert "could not drive this terminal" in text
        assert "pip install" not in text
    else:
        assert "pip install" in text


def test_mcp_groups_tools_by_the_server_in_their_name(tmp_path):
    """The registry itself says which server contributed what — no second source."""
    session = _session_with(tmp_path, tools={
        "mcp__github__create_issue": object(),
        "mcp__github__list_prs": object(),
        "mcp__fs__read": object(),
        "read_file": object(),          # not MCP; must not appear
    })

    text = _run("mcp", "", session).text

    assert "github  (2 tools)" in text
    assert "fs  (1 tools)" in text
    assert "read_file" not in text


def test_mcp_with_nothing_connected_says_where_to_configure_it(tmp_path):
    text = _run("mcp", "", _session_with(tmp_path)).text
    assert "No MCP servers connected" in text
    assert "mcp.json" in text


def test_permissions_lists_what_was_approved(tmp_path):
    session = _session_with(tmp_path, allowlist=_AllowList({"Bash(git *)"}, tmp_path / "a.json"))
    text = _run("permissions", "", session).text

    assert "Bash(git *)" in text
    assert "act" in text


def test_permissions_states_what_cannot_be_pre_approved(tmp_path):
    """The gate is the one thing an allowlist cannot switch off; saying so is
    the difference between a surprise prompt and an expected one."""
    text = _run("permissions", "", _session_with(tmp_path)).text
    assert "cannot be" in text and "pre-approved" in text


def test_copy_with_nothing_to_copy(tmp_path):
    assert "Nothing to copy" in _run("copy", "", _session_with(tmp_path)).text


def test_copy_finds_the_last_assistant_text_through_content_blocks(tmp_path):
    from forge.tui.commands import _last_assistant_text

    session = _session_with(tmp_path, messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "the answer"},
            {"type": "tool_use", "id": "t", "name": "x", "input": {}},
        ]},
    ])
    assert _last_assistant_text(session) == "the answer"


def test_copy_ignores_a_trailing_tool_only_turn(tmp_path):
    """The last assistant message may be tool calls with no prose."""
    from forge.tui.commands import _last_assistant_text

    session = _session_with(tmp_path, messages=[
        {"role": "assistant", "content": "real answer"},
        {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t",
                                           "name": "x", "input": {}}]},
    ])
    assert _last_assistant_text(session) == "real answer"


# ── Attribution, /commit and /review ────────────────────────────────────────


def test_the_agent_identity_becomes_git_environment():
    """Author AND committer. Setting only the author leaves the operator
    recorded as committer of a patch they never saw."""
    from forge.agents.config import GitIdentity

    env = GitIdentity("Optimus Mark II", "optimus@example.com").env()
    assert env["GIT_AUTHOR_NAME"] == "Optimus Mark II"
    assert env["GIT_COMMITTER_NAME"] == "Optimus Mark II"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"]


def test_an_incomplete_identity_sets_nothing():
    """Half an identity would leave git falling back to the operator for the
    other half, which is worse than not touching it at all."""
    from forge.agents.config import GitIdentity

    assert GitIdentity("Optimus", "").env() == {}
    assert GitIdentity("", "a@b.c").env() == {}
    assert GitIdentity().env() == {}


def test_both_shipped_agents_declare_an_identity():
    from forge.agents.registry import AgentRegistry

    registry = AgentRegistry.load()
    for agent in ("optimus", "centurion"):
        identity = registry.get(agent).git
        assert identity.name and identity.email
        assert identity.env()


def test_the_identity_reaches_every_command_in_the_cell():
    """Placed on the Cell rather than in a commit tool, so `run_command git
    commit` — the route an agent actually takes — is attributed too."""
    from forge.cell.base import CellPolicy
    from forge.cell.subprocess_cell import SubprocessCell

    policy = CellPolicy(env={"GIT_AUTHOR_NAME": "Optimus Mark II"})
    cell = SubprocessCell(Path("."), policy)

    assert cell._base_env(None)["GIT_AUTHOR_NAME"] == "Optimus Mark II"  # noqa: SLF001


def test_a_per_call_env_still_wins_over_the_policy():
    from forge.cell.base import CellPolicy
    from forge.cell.subprocess_cell import SubprocessCell

    cell = SubprocessCell(Path("."), CellPolicy(env={"X": "policy"}))
    assert cell._base_env({"X": "call"})["X"] == "call"  # noqa: SLF001


def test_commit_without_a_message_refuses_and_says_how(tmp_path):
    cell = _Cell({"git status --porcelain": _Result(" M a.py\n")})
    out = _run("commit", "", _Session(tmp_path, cell))

    assert "needs a message" in out.text
    assert not any("git commit" in c for c in cell.ran)


def test_commit_with_nothing_staged_says_so(tmp_path):
    out = _run("commit", "msg", _Session(tmp_path, _Cell()))
    assert "Nothing to commit" in out.text


def test_commit_stages_then_commits_and_names_the_author(tmp_path):
    cell = _Cell({"git status --porcelain": _Result(" M a.py\n"),
                  "git commit": _Result("[main abc123] msg\n"),
                  "git log -1": _Result("Optimus Mark II\n")})
    out = _run("commit", "fix the retry", _Session(tmp_path, cell))

    assert any(c == "git add -A" for c in cell.ran)
    assert any("git commit -m" in c for c in cell.ran)
    assert "Optimus Mark II" in out.text


def test_a_quote_in_the_message_cannot_break_the_command(tmp_path):
    cell = _Cell({"git status --porcelain": _Result(" M a.py\n"),
                  "git commit": _Result("[main abc] ok\n")})
    _run("commit", "don't break; rm -rf /", _Session(tmp_path, cell))

    commit = [c for c in cell.ran if c.startswith("git commit")][0]
    assert "'\''" in commit          # the quote was escaped, not closed


def test_review_asks_for_a_turn_rather_than_answering_itself(tmp_path):
    """The review that never happens is the one where the operator has to write
    the request."""
    cell = _Cell({"git diff --stat": _Result(" a.py | 2 +-\n")})
    out = _run("review", "", _Session(tmp_path, cell))

    assert out.prompt, "no turn was requested"
    assert "git diff HEAD" in out.prompt
    assert "a.py" in out.text


def test_review_passes_a_focus_through(tmp_path):
    cell = _Cell({"git diff --stat": _Result(" a.py | 2 +-\n")})
    out = _run("review", "the error handling", _Session(tmp_path, cell))
    assert "the error handling" in out.prompt


def test_review_of_a_clean_tree_starts_no_turn(tmp_path):
    out = _run("review", "", _Session(tmp_path, _Cell()))
    assert not out.prompt
    assert "clean" in out.text


def test_a_plain_command_requests_no_turn(tmp_path):
    assert _run("cwd", "", _Session(tmp_path, _Cell())).prompt == ""
