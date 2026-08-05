"""The input line — history, completion, modes, and the keys that drive them.

What was here before was `input()` on a worker thread. It read a line, and that
was the whole of it: no history, so a mistyped twenty-word prompt had to be
retyped; no completion, so every slash command had to be remembered exactly; no
way to write a second line; no way to run a quick `git status` without spending
a model turn on it.

Built on prompt_toolkit, which supplies the parts that are genuinely hard to get
right across terminals and operating systems — cursor handling, bracketed paste,
reverse search, and a Windows implementation that behaves. Optional: when it is
absent the REPL falls back to plain `input()` with every feature degraded but
nothing broken, the same contract the graph tool uses when Graphify is missing.

**Inline, never full-screen.** prompt_toolkit is used in its inline mode, so
completed turns commit to the terminal's own scrollback. Scroll, select and copy
keep working, output interleaves with shell history, and the transcript is still
on screen after the session ends. A full-screen app would take the alternate
buffer and lose all four — which is why this is not one, and why Claude Code's
own UI is not one either.

Three input modes, distinguished by the first character:

    › fix the retry logic      a prompt for the model
    ! git status               run it in the Cell, no model turn, no tokens
    / compact                  a REPL command
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional — see module docstring
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the fallback path
    AVAILABLE = False
    Completer = object  # type: ignore[assignment,misc]

BASH_PREFIX = "!"
COMMAND_PREFIX = "/"
MENTION_PREFIX = "@"

HISTORY_LIMIT = 2_000
# Directories that are never worth offering as an @-mention. Walking them is
# slow and every hit is noise.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".forge",
              ".forge-worktrees", "dist", "build", ".mypy_cache", ".pytest_cache",
              ".idea", ".vscode", "target", ".next"}
_MENTION_LIMIT = 200


@dataclass(frozen=True)
class Submission:
    """One thing the operator entered, already classified."""
    kind: str          # "prompt" | "bash" | "command" | "eof"
    text: str          # the payload, prefix stripped

    @property
    def is_eof(self) -> bool:
        return self.kind == "eof"


def classify(line: str) -> Submission:
    """Split a raw line into its mode and payload.

    Pure and prefix-only, so it is the same rule everywhere and testable
    without a terminal. A bare prefix with nothing after it is not a mode
    switch — `!` alone is a typo, not a request to run the empty command.
    """
    stripped = line.strip()
    if not stripped:
        return Submission("prompt", "")
    if stripped.startswith(COMMAND_PREFIX) and len(stripped) > 1:
        return Submission("command", stripped)
    if stripped.startswith(BASH_PREFIX) and len(stripped) > 1:
        return Submission("bash", stripped[1:].strip())
    return Submission("prompt", stripped)


def history_path(workspace: Path) -> Path:
    """Per-workspace history: the prompts that make sense in one repo are noise
    in another, and a shared file would leak one project's filenames into
    another's completions."""
    return workspace / ".forge" / "history"


def _walk_files(root: Path, limit: int = _MENTION_LIMIT) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel.replace("\\", "/"))
            if len(out) >= limit:
                return out
    return out


if AVAILABLE:

    class ForgeCompleter(Completer):
        """Slash commands at the start of a line, file paths after an `@`.

        Both are deliberately narrow. Completion that fires everywhere turns
        ordinary prose into a fight with a dropdown, so it triggers only on the
        two prefixes that unambiguously ask for it.
        """

        def __init__(self, commands: dict[str, str], workspace: Path) -> None:
            self._commands = commands
            self._workspace = workspace
            self._files: list[str] | None = None   # scanned lazily, once

        def _file_list(self) -> list[str]:
            if self._files is None:
                try:
                    self._files = _walk_files(self._workspace)
                except OSError:
                    self._files = []
            return self._files

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor

            if text.startswith(COMMAND_PREFIX) and "\n" not in text:
                word = text[1:].lower()
                for name, help_text in sorted(self._commands.items()):
                    if name.startswith(word):
                        yield Completion(name, start_position=-len(word),
                                         display=f"/{name}", display_meta=help_text)
                return

            at = text.rfind(MENTION_PREFIX)
            if at == -1:
                return
            # Only when the @ starts a word — an email address is not a mention.
            if at > 0 and not text[at - 1].isspace():
                return
            fragment = text[at + 1:]
            if "\n" in fragment or " " in fragment:
                return
            lowered = fragment.lower()
            shown = 0
            for rel in self._file_list():
                if lowered in rel.lower():
                    yield Completion(rel, start_position=-len(fragment),
                                     display=rel, display_meta="file")
                    shown += 1
                    if shown >= 20:
                        return


class InputBar:
    """Reads one submission at a time. Owns nothing else."""

    def __init__(self, workspace: Path, commands: dict[str, str],
                 on_cycle_mode=None, on_toggle_expand=None) -> None:
        self.workspace = workspace
        self._commands = commands
        self._on_cycle_mode = on_cycle_mode
        self._on_toggle_expand = on_toggle_expand
        self._session = None
        if AVAILABLE:
            try:
                self._session = self._build_session()
            except Exception:  # noqa: BLE001
                # Importing prompt_toolkit is not the same as being able to
                # drive this terminal. Under Git Bash on Windows it imports
                # fine and then raises NoConsoleScreenBufferError on
                # construction, because TERM says xterm while the console is
                # Win32. There are other variants — a pipe, a dumb TERM, no
                # tty at all — and none of them are worth crashing the session
                # over: the line editor is a convenience, reading a line is
                # not. Fall back and keep going.
                self._session = None

    # ── construction ─────────────────────────────────────────────────────────

    def _build_session(self):
        path = history_path(self.workspace)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(path))
        except OSError:
            history = None            # read-only workspace: run without history

        bindings = KeyBindings()

        @bindings.add("escape", "enter")     # alt+enter / esc-enter
        def _(event) -> None:
            """A newline instead of a submit. Terminals disagree about whether
            shift+enter is even distinguishable, so this is the portable one."""
            event.current_buffer.insert_text("\n")

        @bindings.add("s-tab")
        def _(event) -> None:
            if self._on_cycle_mode is not None:
                self._on_cycle_mode()
                event.app.invalidate()

        @bindings.add("c-o")
        def _(event) -> None:
            if self._on_toggle_expand is not None:
                self._on_toggle_expand()
                event.app.invalidate()

        return PromptSession(
            history=history,
            completer=ForgeCompleter(self._commands, self.workspace),
            key_bindings=bindings,
            editing_mode=EditingMode.EMACS,   # ctrl+r reverse search comes with it
            complete_while_typing=True,
            enable_history_search=True,       # ↑/↓ filter on what is typed
            multiline=False,                  # esc+enter inserts; enter submits
            mouse_support=False,              # keep native terminal selection
        )

    # ── editing mode ─────────────────────────────────────────────────────────

    @property
    def vi_mode(self) -> bool:
        if self._session is None:
            return False
        return self._session.editing_mode == EditingMode.VI

    def set_vi_mode(self, on: bool) -> bool:
        """Switch between emacs and vi key handling. Returns the mode in
        effect, which is False when there is no line editor to configure."""
        if self._session is None:
            return False
        self._session.editing_mode = EditingMode.VI if on else EditingMode.EMACS
        return self.vi_mode

    # ── reading ──────────────────────────────────────────────────────────────

    async def read(self, prompt_ansi: str) -> Submission:
        """One submission. Ctrl-D (or ctrl+C at an empty line) ends the session."""
        if self._session is None:
            return await self._read_plain(prompt_ansi)
        try:
            with patch_stdout(raw=True):
                line = await self._session.prompt_async(ANSI(prompt_ansi))
        except EOFError:
            return Submission("eof", "")
        except KeyboardInterrupt:
            # An empty line means "I am done"; a typed line means "scrap it".
            return Submission("prompt", "")
        return classify(line)

    async def _read_plain(self, prompt_ansi: str) -> Submission:
        import asyncio

        try:
            line = await asyncio.to_thread(input, prompt_ansi)
        except (EOFError, KeyboardInterrupt):
            return Submission("eof", "")
        return classify(line)
