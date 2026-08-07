"""The live region — everything happening right now, on more than one row.

`spinner.py` owns one line and answers three questions: alive, how long, what is
it costing. That was enough while the loop did one thing at a time. It stopped
being enough once the loop grew concurrency it could not show:

    MAX_TOOL_CONCURRENCY = 10      (warden/engine.py)
    MAX_CONCURRENT       = 4       (warden/subagents.py)

A parallel batch rendered as `Running grep` while nine other calls were in
flight and invisible. Subagents were worse than invisible — `render.py` had no
`_on_subagent` handler at all, so a delegation that runs for minutes produced
literally nothing on screen. The harness knew; the operator could not see it.
That is the same failure shape as everything else in RESILIENCE.md, wearing a
different hat.

**Rich is the layout engine; the transient mechanics stay Forge's.** Rich
composes the rows — alignment, truncation, styling, width — which is the part
that is genuinely hard to hand-roll and the reason a hand-rolled TUI reads as
primitive. But it renders to *lines*, and those lines are drawn and erased with
`ansi.rewind`, the same primitive the streamed-prose repaint already uses.

That split is deliberate. `rich.Live` would want to own stdout, and stdout is
already shared with prompt_toolkit's `patch_stdout` and with every direct
`ansi.write` in the renderer. Two owners is a corruption bug waiting for a
parallel batch. Keeping the existing clear-before-write contract — which
`StreamRenderer` already honours on every event — means the region is
generalised from one row to N rows and nothing else changes.

**Never scrolls, and that is load-bearing.** Every frame is fully reclaimed
before the next is drawn. A region that leaks even one row per frame turns a
five-minute turn into a screenful of dead frames with the conversation buried
underneath. The rows are also capped: past `MAX_ROWS`, or past what the terminal
can hold, the region summarises rather than growing, because a rewind taller
than the screen eats scrollback that was never ours.

Degrades to `Spinner` exactly. Without Rich, without a tty, or with colour off,
this is the single line it always was.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from forge.tui import ansi, ui
from forge.tui.spinner import TICK_S, Spinner

MAX_ROWS = 8
"""How many concurrent things the region will name before it summarises.

Ten parallel greps is a legitimate batch and a ten-row live region is a wall.
Past this the tail collapses to `+N more`, which carries the one fact that
matters — that there are more — without the region becoming the screen."""

_HEADROOM = 4
"""Rows kept free below the region. `ansi.rewind` moves the cursor up and erases
downward; if the region fills the terminal the rewind reaches into scrollback
that belongs to the conversation. Better a shorter region than an eaten one."""


@dataclass
class Row:
    """One thing in flight. Finished rows leave the region — they have already
    been committed to scrollback by the renderer, and showing them twice makes
    a batch look like it ran twice."""
    key: str
    label: str
    target: str = ""
    started: float = field(default_factory=time.monotonic)
    kind: str = "tool"          # "tool" | "subagent"
    note: str = ""              # subagents: their current phase

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class LiveRegion:
    """A drop-in for `Spinner` that can draw more than one row.

    Every method `StreamRenderer`, `repl` and the oracle already call is present
    with the same meaning, so it substitutes without touching the callers. The
    additions are the four `*_started` / `*_finished` hooks.
    """

    def __init__(self, *, interruptible: bool = True) -> None:
        # Held for its render() and its timing state — elapsed, token proxy,
        # stall clock, verb rotation — all of which are already right and
        # already tested. This owns the tick and the drawing; the spinner owns
        # the header line's content. Reaching in to swap its draw method would
        # have given two objects one clock and no clear owner.
        self.spinner = Spinner(interruptible=interruptible)
        self._rows: dict[str, Row] = {}
        self._drawn = 0                 # rows currently on screen
        self._overflowed = 0            # rows suppressed by the cap, last frame
        self._task: asyncio.Task | None = None
        self._paused = False

    # ── the Spinner surface, forwarded ───────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            return
        self.spinner.start_clock()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.clear()
        self._rows.clear()

    def pause(self) -> None:
        self._paused = True
        self.clear()

    def resume(self) -> None:
        self._paused = False

    async def _run(self) -> None:
        try:
            while True:
                if not self._paused:
                    self.clear()
                    self._draw()
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken region must not end a turn
            self.clear()

    def set_status(self, status: str) -> None:
        self.spinner.set_status(status)

    def add_chars(self, n: int) -> None:
        self.spinner.add_chars(n)

    @property
    def stalled_for(self) -> float:
        return self.spinner.stalled_for

    # ── what is in flight ────────────────────────────────────────────────────

    def tool_started(self, key: str, label: str, target: str = "") -> None:
        self._rows[key] = Row(key=key, label=label, target=target)

    def tool_finished(self, key: str) -> None:
        """Drop the row. Deliberately says nothing about the outcome: the
        renderer commits the call and its result to scrollback, and a live
        region that also reported it would double every line in the batch."""
        self._rows.pop(key, None)

    def subagent_started(self, key: str, agent: str, label: str = "") -> None:
        self._rows[key] = Row(key=key, label=agent, target=label, kind="subagent")

    def subagent_phase(self, key: str, note: str) -> None:
        row = self._rows.get(key)
        if row is not None:
            row.note = note

    def subagent_finished(self, key: str) -> None:
        self._rows.pop(key, None)

    # ── drawing ──────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Erase the whole region. The contract every caller already honours."""
        if self._drawn <= 0:
            self.spinner.clear()
            return
        ansi.rewind(self._drawn)
        self._drawn = 0

    def rows(self) -> list[str]:
        """The region's body, longest-running first.

        Longest-running first because that is the one most likely to be the
        problem. A batch where nine calls returned in 200 ms and one has been
        going for ninety seconds should lead with the ninety."""
        ordered = sorted(self._rows.values(), key=lambda r: r.started)
        cap = max(1, min(MAX_ROWS, ansi.terminal_height() - _HEADROOM))
        shown, self._overflowed = ordered[:cap], max(0, len(ordered) - cap)

        lines: list[str] = []
        for i, r in enumerate(shown):
            # `└─` only when nothing follows it, including the overflow note —
            # a closing connector with a row under it is worse than no tree.
            last = (i == len(shown) - 1) and not self._overflowed
            lines.append(self._row(r, last=last))
        if self._overflowed:
            lines.append(ansi.paint(f"  └─ … +{self._overflowed} more running", "dim"))
        return lines

    @staticmethod
    def _row(r: Row, *, last: bool) -> str:
        """One row, through Rich when it is there and by hand when it is not.

        The fallback is not a courtesy. `ui` is optional by design — Forge
        installs with three packages and the terminal getting nicer must not be
        why a peer cannot start — so every path through this module has to
        produce something readable without it."""
        subagent = r.kind == "subagent"
        rendered = ui.live_row(r.label, r.target or r.note, r.elapsed,
                               last=last, subagent=subagent)
        if rendered:
            return rendered
        connector = "  └─ " if last else "  ├─ "
        bullet = "◆ " if subagent else "● "
        detail = r.target or r.note
        tail = f"({detail})" if detail and not subagent else (f"  {detail}" if detail else "")
        return ansi.paint(f"{connector}{bullet}{r.label}{tail}  {r.elapsed:.0f}s", "dim")

    def render(self) -> list[str]:
        """Header plus body, as the lines that will be written."""
        return [self.spinner.render(), *self.rows()]

    def _draw(self) -> None:
        """Write the frame. Called by the spinner's tick in place of its own
        single-line draw, so there is one clock and one writer.

        Each row gets a real newline, which is what makes this multi-row at all
        — and is safe only because `clear()` rewinds exactly as many rows as
        were written. That pairing is the entire no-scroll invariant.
        """
        lines = self.render()
        for line in lines:
            ansi.write(line)
        self._drawn = len(lines)
