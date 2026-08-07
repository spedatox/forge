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
from typing import Any
from pathlib import Path

try:  # optional — see module docstring
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.shortcuts import CompleteStyle
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.formatted_text import ANSI, HTML
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

# What a bare `/` offers first. Alphabetical order puts `agent, approved,
# branch, clear, commit, compact` at the top of a six-item menu — none of
# which is what anyone opens the menu to find. Once the operator types a
# letter the filter takes over and this stops mattering.
COMMON_COMMANDS = ("help", "resume", "diff", "status", "commit", "review",
                   "compact", "clear", "cost", "exit")

MAX_SUGGESTIONS = 6
"""How many the menu offers at once.

The reference caps its overlay at five. Past a handful the list stops
being a menu and becomes a page: it covers the transcript, and choosing
from it costs more than typing the name would have."""

HISTORY_LIMIT = 2_000

# ctrl-c twice, inside this window, leaves the session. One press clears the
# line and says so.
#
# ctrl-c ctrl-c is most terminal users' reflex for "get me out", and before this
# it did nothing at all here: the handler returned an empty prompt and the loop
# redrew. Ctrl-d worked and was the only way, which is discoverable only by
# knowing it. The confirmation exists because the same reflex is also how people
# abandon a half-typed line, and exiting on that would lose a session to a
# keystroke meant to clear a typo.
DOUBLE_PRESS_S = 1.5

PASTE_THRESHOLD = 10_000
"""Characters above which an input is folded before it is sent.

Pasting a stack trace, a log, or a CSV at a prompt is an ordinary thing to do,
and until now all of it went to the model verbatim — a 200k-character paste
spends the window in one turn and the only thing that notices is the ledger,
afterwards. The reference draws the same line at the same number."""

PASTE_KEEP = 500
"""Kept from each end. Enough that the head still identifies what was pasted and
the tail still shows how it ended, which between them is what a person actually
refers to when they paste something and ask about it."""


def fold_paste(text: str, spill_path: Path | None = None,
               threshold: int = PASTE_THRESHOLD,
               keep: int = PASTE_KEEP) -> tuple[str, str]:
    """Split an oversized input into (what to send, what was withheld).

    Returns the text unchanged and an empty remainder when it is under the
    threshold, so callers can apply it unconditionally.

    The placeholder counts the elided LINES rather than characters, because
    lines are the unit a person paste-estimates in — "+4,000 lines" lands, and
    "+182,331 characters" does not.

    When `spill_path` is given the FULL text is written there and the
    placeholder names it. Withholding text and saying nothing about where it
    went would make the message a quiet lie about what was provided; a path
    turns the elision into something the model can undo with one `read_file`.
    It is the same contract oversized tool results already have.
    """
    if len(text) <= threshold:
        return text, ""
    head, tail = text[:keep], text[-keep:]
    middle = text[keep:-keep]
    lines = middle.count("\n") + 1

    where = ""
    if spill_path is not None:
        try:
            spill_path.parent.mkdir(parents=True, exist_ok=True)
            spill_path.write_text(text, encoding="utf-8")
            where = f" — the full text is at {spill_path}"
        except OSError:
            where = ""     # unwritable workspace: fold anyway, say less
    return (f"{head}\n\n[… {lines:,} lines elided from this paste{where} …]\n\n{tail}",
            middle)
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


def _menu_order(item: tuple[str, str]) -> tuple[int, str]:
    """Commonly reached-for commands first, then alphabetical."""
    name = item[0]
    try:
        return (COMMON_COMMANDS.index(name), "")
    except ValueError:
        return (len(COMMON_COMMANDS), name)


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
                shown = 0
                for name, help_text in sorted(self._commands.items(),
                                              key=_menu_order):
                    if not name.startswith(word):
                        continue
                    yield Completion(name, start_position=-len(word),
                                     display=f"/{name}", display_meta=help_text)
                    shown += 1
                    if shown >= MAX_SUGGESTIONS:
                        return
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
                    if shown >= MAX_SUGGESTIONS:
                        return


_MENU_STYLE = None
if AVAILABLE:
    from prompt_toolkit.styles import Style as _PTStyle

    # Greys, like everything else. The selected row is the only bright
    # thing in the menu, which is what makes it findable at a glance.
    _MENU_STYLE = _PTStyle.from_dict({
        "completion-menu": "bg:#1c1c1c #b2b2b2",
        "completion-menu.completion": "bg:#1c1c1c #b2b2b2",
        "completion-menu.completion.current": "bg:#3a3a3a #ffffff bold",
        "completion-menu.meta.completion": "bg:#1c1c1c #7f7f7f",
        "completion-menu.meta.completion.current": "bg:#3a3a3a #d0d0d0",
        "bottom-toolbar": "noreverse",
    })


class InputBar:
    """Reads one submission at a time. Owns nothing else."""

    def __init__(self, workspace: Path, commands: dict[str, str],
                 on_cycle_mode=None, on_toggle_expand=None,
                 hint: Any = None) -> None:
        self.workspace = workspace
        self._hint = hint
        self._commands = commands
        self._on_cycle_mode = on_cycle_mode
        self._on_toggle_expand = on_toggle_expand
        self._session = None
        self._last_interrupt = 0.0
        self.degraded_reason = "" if AVAILABLE else "prompt_toolkit is not installed"
        if AVAILABLE:
            try:
                self._session = self._build_session()
            except Exception as first:  # noqa: BLE001
                # prompt_toolkit's create_output() checks sys.platform: on
                # Windows it uses Win32Output, which needs a real Windows
                # console buffer. Under Git Bash / MSYS2 / Cygwin the terminal
                # is a PTY that speaks ANSI — Win32Output raises
                # NoConsoleScreenBufferError, but Vt100_Output works. Retry
                # with that before giving up.
                self._session = self._build_session_vt100()
                if self._session is None:
                    # Everything else (a pipe, a dumb TERM, no tty) degrades to
                    # plain input() — the line editor is a convenience, reading
                    # a line is not. But it degrades LOUDLY now. Silently
                    # falling back cost a whole investigation: the prompt looks
                    # identical, so the missing dropdown reads as a completion
                    # bug rather than as "there is no line editor here", and
                    # multi-line paste breaks for the same invisible reason.
                    self.degraded_reason = f"{type(first).__name__}: {first}"

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

        # Deleting has to re-offer completions. prompt_toolkit runs the
        # completer on insert but `delete_before_cursor` does not touch it,
        # so backspacing over a typo left the previous menu on screen —
        # stale, and describing text that is no longer there. Correcting a
        # mistyped command is exactly when the menu is wanted most.
        @bindings.add("backspace")
        def _(event) -> None:
            event.current_buffer.delete_before_cursor(count=event.arg)
            event.current_buffer.start_completion(select_first=False)

        @bindings.add("delete")
        def _(event) -> None:
            event.current_buffer.delete(count=event.arg)
            event.current_buffer.start_completion(select_first=False)

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

        def _toolbar():
            """The line under the input.

            Where the reference TUI puts its shortcut hints, and the reason
            is the same: the keys that drive this are invisible from a bare
            cursor, and a hint sitting under the place you are typing is
            read without being looked for.
            """
            text = self._hint() if callable(self._hint) else self._hint
            return ANSI(text) if text else None

        return PromptSession(
            history=history,
            bottom_toolbar=_toolbar,
            completer=ForgeCompleter(self._commands, self.workspace),
            key_bindings=bindings,
            editing_mode=EditingMode.EMACS,   # ctrl+r reverse search comes with it
            complete_while_typing=True,
            # A single vertical list, the shape the reference uses: name,
            # then its description dimmed beside it. A grid packs more in
            # and gives each entry nowhere to explain itself, which is the
            # whole reason to show a menu rather than expect recall.
            complete_style=CompleteStyle.COLUMN,
            style=_MENU_STYLE,
            # This is not cosmetic padding — it is what makes the menu appear
            # at all. prompt_toolkit only guarantees the layout a minimum
            # height when this is NON-ZERO:
            #
            #     space = self.reserve_space_for_menu
            #     if space and not get_app().is_done:      # 0 is falsy
            #         return Dimension(min=space)
            #     return Dimension()
            #
            # I set it to 0 to kill the dead space at the default of 8, and
            # took the menu with it. It survived review because a menu with
            # room below the cursor still draws: on a fresh screen the prompt
            # sits mid-terminal and completion looked fine. Once the transcript
            # grew and the prompt reached the last row, there was nowhere to
            # draw and it silently stopped — which is exactly how it was
            # reported, as having "stopped working".
            #
            # So it reserves what the menu can actually use, and no more: the
            # same cap the completer yields.
            reserve_space_for_menu=MAX_SUGGESTIONS,
            # NOT enable_history_search. prompt_toolkit disables
            # complete_while_typing whenever it is on — its own source says
            # so — and that silently cost the menu that appears on `/` and
            # `@`. It only makes ↑ filter by prefix; ↑/↓ and ctrl+r work
            # without it, and nobody should have to remember command names
            # to save a filtered history walk.
            multiline=False,                  # esc+enter inserts; enter submits
            mouse_support=False,              # keep native terminal selection
        )

    def _build_session_vt100(self):
        """Retry with explicit Vt100_Output for Git Bash / MSYS2 on Windows.

        prompt_toolkit's create_output() routes to Win32Output when
        sys.platform is 'win32', but terminals like mintty (Git Bash) and
        MSYS2 provide a PTY that speaks ANSI escapes — there is no Windows
        console buffer to attach to.  Creating an app session with an explicit
        Vt100_Output before building the PromptSession lets prompt_toolkit
        drive the terminal it actually has."""
        import sys

        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.output.vt100 import Vt100_Output

        try:
            out = Vt100_Output.from_pty(sys.stdout)
            # ENTERED, not just constructed. create_app_session is a
            # @contextmanager: assigning it runs none of its body, so the
            # retry would have executed in exactly the environment that just
            # failed and failed identically. Kept on the instance because the
            # session it installs must outlive this method — every later
            # read() runs inside it.
            self._vt100_ctx = create_app_session(output=out)
            self._vt100_ctx.__enter__()
            return self._build_session()
        except Exception:  # noqa: BLE001
            ctx = getattr(self, "_vt100_ctx", None)
            if ctx is not None:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                self._vt100_ctx = None
            return None

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
        """One submission. Ctrl-D ends the session; so does ctrl-C twice.

        The docstring here used to claim ctrl-C at an empty line ended the
        session. It did not — the handler returned an empty prompt and the loop
        redrew, so the only exit was ctrl-D and the only way to find it was to
        already know. Now the reflex works, with a confirmation, because the
        same keystroke is how people abandon a half-typed line.
        """
        if self._session is None:
            return await self._read_plain(prompt_ansi)
        try:
            with patch_stdout(raw=True):
                line = await self._session.prompt_async(ANSI(prompt_ansi))
        except EOFError:
            return Submission("eof", "")
        except KeyboardInterrupt:
            if self._interrupt_again():
                return Submission("eof", "")
            return Submission("prompt", "")
        self._last_interrupt = 0.0     # any real submission ends the streak
        return classify(line)

    def _interrupt_again(self) -> bool:
        """True when this ctrl-C is the second within the window.

        Prints the confirmation itself rather than returning a flag for the
        caller to render: the prompt is about to be redrawn over this line, so
        the notice has to be written before returning or it never appears.
        """
        import time

        now = time.monotonic()
        if now - self._last_interrupt <= DOUBLE_PRESS_S:
            self._last_interrupt = 0.0
            return True
        self._last_interrupt = now
        print("  press ctrl-c again to exit, or ctrl-d", flush=True)
        return False

    async def _read_plain(self, prompt_ansi: str) -> Submission:
        import asyncio

        try:
            line = await asyncio.to_thread(input, prompt_ansi)
        except (EOFError, KeyboardInterrupt):
            return Submission("eof", "")
        return classify(line)
