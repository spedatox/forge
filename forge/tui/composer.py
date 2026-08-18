"""Typing while the agent works.

Forge's prompt lived only between turns. If the agent set off in the wrong
direction at iteration two, the operator had two options: sit through forty more
iterations, or ctrl+c and lose the turn. Both references treat this as a core
capability rather than a nicety — Codex keeps its composer live under a running
task, DSH ships `steer`/`followup`/`inject` over one primitive — and it is the
single clearest UX gap this harness had.

**It reuses `keys.read_key`'s machinery, not prompt_toolkit.** prompt_toolkit
wants to own the bottom of the screen and stdout with it, and the live region
already owns both. Two owners is a corruption bug waiting for a parallel batch.
`keys` already reads one key at a time with a deadline, on Windows and POSIX,
and already degrades honestly where it cannot — which is exactly the contract
this needs.

**The draft renders as a row of the live region.** It is not written directly.
The region redraws every tick and reclaims exactly what it drew, so a composer
row inherits the whole no-scroll invariant for free; writing the line itself
would put a second writer on stdout and reintroduce the problem the region
exists to solve.

**Ctrl+c is read as a key here, not delivered as a signal.** A tty in raw mode
does not raise SIGINT — `\\x03` arrives as input. So while the composer is
polling, the SIGINT handler installed for the turn never fires, and this becomes
the path that carries an interrupt. It calls the same abort as the handler, so
the two are indistinguishable from the loop's side. Getting this wrong would
mean ctrl+c silently stopped working the moment the operator could type, which
is the exact moment they are most likely to reach for it.

**Nothing typed is ever lost.** Text still in the draft when a turn ends is
handed back, and text queued but never claimed comes back too — an operator who
typed a sentence and watched the turn finish first gets it as their next prompt
rather than watching it vanish.
"""
from __future__ import annotations

import asyncio
import logging

from forge.tui import ansi, keys, ui

logger = logging.getLogger("forge.tui")

_POLL_S = 0.05
"""How long each key read waits before returning to the loop.

Short enough that an abort is noticed promptly, long enough that idling costs
nothing measurable. The read is on a worker thread either way; this bounds how
long that thread is parked, which is what makes the composer cancellable."""

_MAX_DRAFT = 4_000
"""Characters one draft may hold. Past this the operator is pasting a file, and
a file belongs in the workspace where `read_file` can reach it — a wall of text
in a one-line composer is unreadable to them and unbounded for the region."""

_BACKSPACE = ("\x7f", "\b")


class Composer:
    """A one-line editor that runs alongside a turn.

    Owns no output of its own: `line()` is called by the live region while it
    composes its frame. The only things this touches are its own draft and the
    inbox it pushes into."""

    def __init__(self, inbox, *, on_abort=None) -> None:
        self.inbox = inbox
        self.on_abort = on_abort
        self.draft = ""
        self.sent = 0
        self._task: asyncio.Task | None = None
        self._enabled = keys.available()
        """False on a pipe, a dumb terminal, or a platform with neither reader.
        The turn still runs; there is simply no composer, which is exactly the
        behaviour that existed before this module."""

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is not None or not self._enabled:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> str:
        """Stop polling and hand back anything still in the draft.

        Returned rather than discarded because a half-typed sentence is work the
        operator did, and losing it to a turn that happened to end first is the
        kind of small theft that stops people trusting the input line."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        leftover, self.draft = self.draft, ""
        return leftover

    # ── what the live region draws ───────────────────────────────────────────
    def line(self) -> str | None:
        """The composer row, or None when there is nothing to show.

        Hidden until the first keypress. An always-present empty input line
        under a running turn reads as a prompt waiting for an answer, and an
        operator who thinks the agent is blocked on them will stop watching it
        work."""
        if not self._enabled:
            return None
        if not self.draft and not self.sent:
            return None
        if not self.draft:
            note = f"{self.sent} queued — it will be picked up at the next step"
            rendered = ui.composer_line("", note, queued=self.sent)
            return rendered or ansi.paint(f"  ┆ {note}", "dim")

        rendered = ui.composer_line(self.draft, "", queued=self.sent)
        if rendered:
            return rendered
        body = ansi.truncate(f"  ┆ {self.draft}▏",
                             max(20, ansi.terminal_width() - 1))
        return ansi.paint(body, "cyan")

    # ── the read loop ────────────────────────────────────────────────────────
    async def _run(self) -> None:
        try:
            while True:
                key = await asyncio.to_thread(keys.read_key_raw, _POLL_S)
                if key is None:
                    return              # this terminal will never deliver keys
                if key == keys.NOTHING:
                    continue
                self._feed(key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a broken composer must not end a turn
            logger.warning("composer_failed", extra={"error": repr(e)})

    def _feed(self, key: str) -> None:
        if key == keys.CANCEL:
            # Raw mode swallowed the signal, so this IS the interrupt. A draft
            # in progress is cleared first and the abort only fires on an empty
            # line — the same two-stage escape a shell gives you, and it means
            # ctrl+c after a typo costs the typo rather than the turn.
            if self.draft:
                self.draft = ""
                return
            if self.on_abort is not None:
                self.on_abort()
            return
        if key == keys.ENTER:
            if self.draft.strip():
                self.inbox.push(self.draft)
                self.sent += 1
            self.draft = ""
            return
        if key in _BACKSPACE:
            self.draft = self.draft[:-1]
            return
        if len(key) != 1 or not key.isprintable():
            return                      # arrows, function keys, control codes
        if len(self.draft) >= _MAX_DRAFT:
            return
        self.draft += key
