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


# ── Driving the line editor without a terminal ──────────────────────────────
# InputBar swallows construction failures by design (a terminal it cannot drive
# must not end the session), so on a dev machine `_session` is None and every
# key-binding test silently skips. That is how a broken assertion reached CI.
# A pipe input plus DummyOutput gives prompt_toolkit something it will build
# against anywhere, so these run on Windows, in Git Bash and on Linux CI alike.


def _headless_bar(workspace: Path) -> InputBar:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        bar = InputBar(workspace, command_help())
    if bar._session is None:                       # noqa: SLF001
        pytest.skip("prompt_toolkit would not build even headlessly")
    return bar


def _handler_for(workspace: Path, key_name: str):
    """The bound handler for a key, found the way prompt_toolkit resolves it.

    Looked up through KEY_ALIASES rather than by string-matching the key's
    name: `backspace` is an alias for `c-h` and is stored as `Keys.ControlH`,
    so searching names for "backspace" finds nothing.
    """
    from prompt_toolkit.keys import KEY_ALIASES, Keys

    bar = _headless_bar(workspace)
    target = Keys(KEY_ALIASES.get(key_name, key_name))
    for binding in bar._session.key_bindings.bindings:   # noqa: SLF001
        if len(binding.keys) == 1 and binding.keys[0] == target:
            return binding.handler
    raise AssertionError(f"{key_name} ({target}) is not bound")


class _RecordingBuffer:
    """Just enough Buffer for a delete handler to run against."""

    def __init__(self) -> None:
        self.deleted = 0
        self.completion_started = False

    def delete_before_cursor(self, count: int = 1) -> str:
        self.deleted += count
        return ""

    def delete(self, count: int = 1) -> str:
        self.deleted += count
        return ""

    def start_completion(self, select_first: bool = True) -> None:
        self.completion_started = True


class _FakeKeyEvent:
    def __init__(self, buffer: _RecordingBuffer) -> None:
        self.current_buffer = buffer
        self.arg = 1


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


# ── The menu has to actually appear ─────────────────────────────────────────


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_completion_fires_while_typing(tmp_path):
    """The dropdown is the whole point of having a completer.

    prompt_toolkit disables complete_while_typing whenever
    enable_history_search is on — its own source says so — and that silently
    cost the menu on `/` and `@`. History search only makes ↑ filter by prefix;
    nobody should have to remember command names to buy that.
    """
    bar = InputBar(tmp_path, command_help())
    if bar._session is None:                       # noqa: SLF001
        pytest.skip("no line editor in this terminal")

    assert bar._session.complete_while_typing      # noqa: SLF001
    assert not bar._session.enable_history_search  # noqa: SLF001


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_a_bare_slash_offers_the_useful_ones_first(tmp_path):
    """The menu is capped, so what fills it matters.

    Alphabetically the first six are `agent, approved, branch, clear, commit,
    compact` — none of which is what anyone opens the menu to find. Past a
    handful a menu stops being a menu and becomes a page that covers the
    transcript."""
    from prompt_toolkit.document import Document

    from forge.tui.input import MAX_SUGGESTIONS, ForgeCompleter

    completer = ForgeCompleter(command_help(), tmp_path)
    offered = [c.text for c in completer.get_completions(Document("/"), None)]

    assert len(offered) <= MAX_SUGGESTIONS
    assert offered[0] == "help"
    assert "resume" in offered
    assert "agent" not in offered, "alphabetical order won over usefulness"


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_typing_a_letter_narrows_to_matches(tmp_path):
    """Once there is a filter the ordering stops mattering — only matches."""
    from prompt_toolkit.document import Document

    from forge.tui.input import ForgeCompleter

    completer = ForgeCompleter(command_help(), tmp_path)
    offered = [c.text for c in completer.get_completions(Document("/re"), None)]

    assert set(offered) == {"resume", "review"}


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_file_suggestions_are_capped_too(tmp_path):
    """A monorepo would otherwise bury the transcript under filenames."""
    from prompt_toolkit.document import Document

    from forge.tui.input import MAX_SUGGESTIONS, ForgeCompleter

    for i in range(40):
        (tmp_path / f"mod{i}.py").write_text("x", encoding="utf-8")
    completer = ForgeCompleter({}, tmp_path)

    offered = list(completer.get_completions(Document("see @mod"), None))
    assert len(offered) <= MAX_SUGGESTIONS


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_a_bare_at_offers_files(tmp_path):
    from prompt_toolkit.document import Document

    from forge.tui.input import ForgeCompleter

    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.py").write_text("x", encoding="utf-8")
    completer = ForgeCompleter({}, tmp_path)

    offered = [c.text for c in completer.get_completions(Document("look at @"), None)]
    assert {"alpha.py", "beta.py"} <= set(offered)


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_each_command_completion_carries_its_summary(tmp_path):
    """The menu explains itself, so choosing does not need prior knowledge."""
    from prompt_toolkit.document import Document

    from forge.tui.input import ForgeCompleter

    completer = ForgeCompleter(command_help(), tmp_path)
    resume = [c for c in completer.get_completions(Document("/resume"), None)][0]

    assert resume.display_meta_text.strip()


# ── The hint bar under the input ────────────────────────────────────────────


def test_the_hint_names_the_keys_that_cannot_be_guessed():
    """A bare cursor gives no indication that ! or @ mean anything."""
    from forge.tui.repl import _hint_line

    class _S:
        permission_mode = "act"

    line = _hint_line(_S())
    assert "!cmd" in line and "@file" in line and "/help" in line


def test_the_hint_changes_with_the_mode():
    """Plan mode silently governs whether the next request can write anything,
    which makes it the one piece of state worth carrying under the cursor."""
    from forge.tui.repl import _hint_line

    class _Act:
        permission_mode = "act"

    class _Plan:
        permission_mode = "plan"

    assert "shift+tab" in _hint_line(_Act())
    assert "denied" in _hint_line(_Plan())


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_the_bar_accepts_a_hint_callable(tmp_path):
    """Evaluated per keystroke, not captured once — the mode it reports can
    change mid-session via shift+tab."""
    calls = {"n": 0}

    def _hint():
        calls["n"] += 1
        return "  hint"

    bar = InputBar(tmp_path, command_help(), hint=_hint)
    if bar._session is None:                      # noqa: SLF001
        pytest.skip("no line editor in this terminal")

    assert bar._session.bottom_toolbar is not None  # noqa: SLF001


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
@pytest.mark.parametrize("key_name", ["backspace", "delete"])
def test_deleting_re_offers_completions(tmp_path, key_name):
    """prompt_toolkit runs the completer on insert, but `delete_before_cursor`
    never touches it — so backspacing over a typo left the previous menu on
    screen, stale, describing text no longer there. Correcting a mistyped
    command is exactly when the menu is wanted most.

    This asserts the handler actually asks for completions, rather than that a
    binding exists under a particular name. The earlier version searched the
    bound key names for "backspace" and could never have passed: prompt_toolkit
    aliases `backspace` to `c-h` (KEY_ALIASES) and stores it as
    `Keys.ControlH`, whose name contains no "backspace" at all. It went
    unnoticed because it skipped on the dev machine — Git Bash has no console
    prompt_toolkit can drive — and only ran, and failed, on CI.
    """
    handler = _handler_for(tmp_path, key_name)
    buffer = _RecordingBuffer()
    handler(_FakeKeyEvent(buffer))

    assert buffer.completion_started, (
        f"{key_name} does not refresh the menu, so correcting a typo leaves a "
        "stale one on screen"
    )
    assert buffer.deleted, f"{key_name} no longer deletes anything"


@pytest.mark.skipif(not AVAILABLE, reason="prompt_toolkit not installed")
def test_no_rows_are_reserved_for_the_menu(tmp_path):
    """Reserved rows are permanent dead space. At the default of eight the hint
    bar sat in mid-screen; at one the menu had room for a single entry and read
    as broken. Zero reserves nothing and lets the menu take what it needs."""
    bar = InputBar(tmp_path, command_help())
    if bar._session is None:                       # noqa: SLF001
        pytest.skip("no line editor in this terminal")

    assert bar._session.reserve_space_for_menu == 0   # noqa: SLF001
