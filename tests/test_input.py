"""The input line.

What was here before was `input()` on a worker thread — no history, no
completion, no second line, and no way to run `git status` without spending a
model turn on it.

Most of what the new layer does needs a terminal, so what is tested here is the
part that does not: the mode rule, the completion sources, and the fallback.
Those are the pieces where a mistake is silent — a prefix rule that swallows an
ordinary sentence, or a completer that offers a command that no longer exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forge.tui.commands import command_help
from forge.tui.input import (
    AVAILABLE, InputBar, Submission, _walk_files, classify, history_path,
)


# ── Mode classification ─────────────────────────────────────────────────────


@pytest.mark.parametrize("line,kind,text", [
    ("fix the retry logic", "prompt", "fix the retry logic"),
    ("  fix it  ", "prompt", "fix it"),
    ("/compact", "command", "/compact"),
    ("/model gpt", "command", "/model gpt"),
    ("!git status", "bash", "git status"),
    ("! ls -la", "bash", "ls -la"),
])
def test_a_line_is_classified_by_its_prefix(line, kind, text):
    result = classify(line)
    assert (result.kind, result.text) == (kind, text)


@pytest.mark.parametrize("line", ["!", "/", " ! ", ""])
def test_a_bare_prefix_is_not_a_mode_switch(line):
    """`!` alone is a typo, not a request to run the empty command."""
    assert classify(line).kind == "prompt"


def test_prose_containing_a_prefix_is_still_prose():
    """Only the FIRST character decides — otherwise ordinary English breaks."""
    for line in ("what does ! mean in bash",
                 "the path is a/b/c",
                 "email me at a@b.com"):
        assert classify(line).kind == "prompt"


def test_eof_is_its_own_kind():
    assert Submission("eof", "").is_eof
    assert not classify("anything").is_eof


# ── History ─────────────────────────────────────────────────────────────────


def test_history_is_per_workspace(tmp_path):
    """One repo's prompts are noise in another, and a shared file would leak
    one project's filenames into another's completions."""
    a, b = tmp_path / "a", tmp_path / "b"
    assert history_path(a) != history_path(b)
    assert history_path(a).parent.name == ".forge"


# ── @-mention file scanning ─────────────────────────────────────────────────


def test_file_scan_skips_noise_directories(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    for junk in (".git", "node_modules", "__pycache__", ".venv"):
        d = tmp_path / junk
        d.mkdir()
        (d / "junk.py").write_text("x", encoding="utf-8")

    found = _walk_files(tmp_path)

    assert "src/app.py" in found
    assert not any("node_modules" in f or ".git" in f or "__pycache__" in f
                   for f in found)


def test_file_scan_is_bounded(tmp_path):
    """A monorepo must not stall the first keystroke after an @."""
    for i in range(50):
        (tmp_path / f"f{i}.py").write_text("x", encoding="utf-8")
    assert len(_walk_files(tmp_path, limit=10)) == 10


def test_file_scan_uses_forward_slashes(tmp_path):
    """Completions are pasted into prompts and shell commands; a backslash on
    Windows would be read as an escape."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    assert "pkg/mod.py" in _walk_files(tmp_path)


# ── Completion is driven by the live registry ───────────────────────────────


def test_command_help_covers_the_real_registry():
    """Built from the registry so a new command completes by existing —
    a second hand-kept list would drift the first time one was added."""
    help_map = command_help()
    assert "compact" in help_map and "cost" in help_map
    assert all(isinstance(v, str) and v for v in help_map.values())


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_slash_completion_offers_matching_commands(tmp_path):
    from prompt_toolkit.document import Document

    from forge.tui.input import ForgeCompleter

    completer = ForgeCompleter(command_help(), tmp_path)
    out = [c.text for c in completer.get_completions(Document("/co"), None)]

    assert "compact" in out and "cost" in out
    assert "help" not in out


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_mention_completion_offers_files(tmp_path):
    from prompt_toolkit.document import Document

    from forge.tui.input import ForgeCompleter

    (tmp_path / "retry.py").write_text("x", encoding="utf-8")
    completer = ForgeCompleter({}, tmp_path)

    out = [c.text for c in completer.get_completions(Document("look at @ret"), None)]
    assert "retry.py" in out


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_an_email_address_does_not_trigger_file_completion(tmp_path):
    from prompt_toolkit.document import Document

    from forge.tui.input import ForgeCompleter

    (tmp_path / "b.py").write_text("x", encoding="utf-8")
    completer = ForgeCompleter({}, tmp_path)

    out = list(completer.get_completions(Document("mail me at a@b"), None))
    assert out == []


# ── Degradation ─────────────────────────────────────────────────────────────


def test_the_bar_constructs_without_prompt_toolkit(monkeypatch, tmp_path):
    """Absent the dependency the REPL still runs, every feature degraded but
    nothing broken — the same contract the graph tool uses."""
    import forge.tui.input as mod

    monkeypatch.setattr(mod, "AVAILABLE", False)
    bar = InputBar(tmp_path, {})
    assert bar._session is None


def test_a_terminal_prompt_toolkit_cannot_drive_falls_back(monkeypatch, tmp_path):
    """Importing it is not the same as being able to drive this terminal.

    Under Git Bash on Windows it imports fine and then raises
    NoConsoleScreenBufferError on construction, because TERM says xterm while
    the console is Win32. Caught in the wild by this test. The line editor is a
    convenience; reading a line is not, so any construction failure degrades
    instead of ending the session.
    """
    import forge.tui.input as mod

    def _boom(self):
        raise RuntimeError("Found xterm-256color, while expecting a Windows console")

    monkeypatch.setattr(mod.InputBar, "_build_session", _boom)
    bar = InputBar(tmp_path, command_help())
    assert bar._session is None


def test_a_read_only_workspace_does_not_stop_the_session(monkeypatch, tmp_path):
    """History is a convenience; failing to open it must not be fatal."""
    import forge.tui.input as mod

    if not AVAILABLE:
        pytest.skip("prompt_toolkit not installed")

    real_mkdir = Path.mkdir

    def _boom(self, *a, **k):
        if ".forge" in str(self):
            raise OSError("read-only file system")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _boom)
    # Constructing must not raise; whether a session is built depends on the
    # terminal, which is not what this test is about.
    InputBar(tmp_path, command_help())
