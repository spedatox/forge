"""The live line — what the agent is doing, right now.

A turn can run for minutes: the model thinks, tools run, a retry backs off, a
compaction summarizes. Without a live line all of that is a blank terminal, and
a blank terminal is indistinguishable from a hang. The operator's next move —
wait, or interrupt — depends on telling those apart, so the line exists to
answer three questions continuously: is it alive, how long has it been, and what
is it costing.

    ✻ Pondering… (esc to interrupt · 32s · ↓ 1.2k tokens)

Modelled on Claude Code's spinner, including the rotating verb. That is not
decoration: a spinner whose text never changes reads as frozen after about
twenty seconds, and the verb turning over is the cheapest possible proof that
the loop is still going round.

**It owns exactly one line and never scrolls.** Drawn with `ansi.transient`, so
each frame overwrites the last. Any real output must call `clear()` first —
`StreamRenderer` does — and the next tick redraws underneath it. That ordering
is the whole contract; get it wrong and prose interleaves with spinner frames.

Silent when styling is off. Piping `forge chat` into a file should produce a
transcript, not ten thousand escape sequences.
"""
from __future__ import annotations

import asyncio
import time

from forge.tui import ansi

FRAMES = ("◐", "◓", "◑", "◒")
FALLBACK_FRAMES = ("|", "/", "-", "\\")

# Rotated slowly, so the line reads as considered rather than frantic.
VERBS = (
    "Thinking", "Pondering", "Working", "Reasoning", "Considering",
    "Digging", "Weighing", "Chewing", "Puzzling", "Mulling",
)

TICK_S = 0.12
VERB_EVERY_S = 6.0
# Below this a count is noise; the operator cares about thousands, not bytes.
_TOKEN_FLOOR = 200

# How long nothing may arrive before the line says so.
#
# Every spinner answers "am I alive". Only this answers "is anything actually
# arriving" — and a rotating verb proves the event loop is turning, which is
# exactly what stays true when the provider has silently stopped sending. The
# operator's next move, wait or interrupt, depends on telling those apart, and
# without this they render identically.
#
# Suppressed while a tool is running: a two-minute test suite is not a stall,
# and colouring it red teaches the eye to ignore the colour, which costs the
# signal in the one case it was built for.
STALL_AFTER_S = 4.0


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class Spinner:
    """One live line for one turn. Start it, feed it, stop it."""

    def __init__(self, *, interruptible: bool = True) -> None:
        self._task: asyncio.Task | None = None
        self._started = 0.0
        self._chars = 0
        self._status = ""
        self._interruptible = interruptible
        self._visible = False
        self._paused = False
        self._last_progress = 0.0
        """When something last actually arrived. Reset by streamed characters
        and by any status change — a tool starting is progress even though no
        token came with it."""
        self._tool_running = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start_clock(self) -> None:
        """Begin timing without starting a draw loop.

        For `LiveRegion`, which draws the header itself as one row of a larger
        frame. Two objects drawing on one clock is how a frame ends up half
        from each; this keeps the timing here — where `render`, the stall
        detector and the verb rotation all read it — and the drawing there."""
        self._started = time.monotonic()
        self._last_progress = self._started

    def start(self) -> None:
        if self._task is not None:
            return
        self.start_clock()
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

    def clear(self) -> None:
        """Erase the line so something real can be printed over it."""
        if self._visible:
            ansi.clear_transient()
            self._visible = False

    def pause(self) -> None:
        """Stop drawing until something asks for it again.

        Required the moment the model starts streaming prose. Streamed text
        carries no newline until the turn ends, so the cursor sits partway
        along a line the operator is reading — and the next frame's `\r`
        returns to column 0 and overwrites it. The visible symptom is a
        reply that begins in the middle of its own first word."""
        self._paused = True
        self.clear()

    def resume(self) -> None:
        """Draw again — the turn went back to work after speaking."""
        self._paused = False

    # ── what it reports ──────────────────────────────────────────────────────

    def add_chars(self, n: int) -> None:
        """Streamed response characters, used as a rough token proxy.

        Rough on purpose: the exact count arrives with the usage report at the
        END of the turn, which is precisely when it stops being useful. ~4 chars
        per token is close enough to answer "is this getting expensive".
        """
        self._chars += max(0, n)
        if n > 0:
            self._last_progress = time.monotonic()

    def set_status(self, status: str) -> None:
        """A short note replacing the verb — a running tool, a compaction, a
        retry. What the harness is doing beats a generic verb every time.

        Counts as progress, and sets the tool flag when it names one. A tool
        that takes four minutes is working, not stalled, and the only thing
        that knows the difference is which status was last set."""
        self._status = status.strip()
        self._last_progress = time.monotonic()
        self._tool_running = self._status.startswith("Running ")

    @property
    def stalled_for(self) -> float:
        """Seconds since anything arrived, or 0 while a tool is running."""
        if self._tool_running or not self._started:
            return 0.0
        return max(0.0, time.monotonic() - self._last_progress)

    # ── drawing ──────────────────────────────────────────────────────────────

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    def render(self, now: float | None = None, stalled_for: float | None = None) -> str:
        t = self.elapsed_s if now is None else now
        stalled = self.stalled_for if stalled_for is None else stalled_for
        frames = FRAMES if ansi.unicode_ok() else FALLBACK_FRAMES
        frame = frames[int(t / TICK_S) % len(frames)]
        label = self._status or VERBS[int(t / VERB_EVERY_S) % len(VERBS)]

        bits = []
        if self._interruptible:
            bits.append("ctrl-c to interrupt")
        bits.append(f"{int(t)}s")
        tokens = self._chars // 4
        if tokens >= _TOKEN_FLOOR:
            bits.append(f"↓ {_humanize_tokens(tokens)} tokens")

        # Stalled: the glyph and the verb both change colour, and the tail says
        # how long the silence has lasted. Saying it in words as well as colour
        # is not redundancy — colour is off in a pipe, on a dumb TERM, and under
        # NO_COLOR, which are the same places a hung connection is hardest to
        # diagnose.
        if stalled >= STALL_AFTER_S:
            bits.append(f"nothing received for {int(stalled)}s")
            return (ansi.paint(f"  {frame} ", "yellow")
                    + ansi.paint(f"{label}…", "yellow")
                    + ansi.paint(f"  ({' · '.join(bits)})", "dim"))

        # Grey, like the rows under it. The live region is one object and a
        # coloured header on grey rows reads as two. It also follows the
        # palette rule the rest of the TUI keeps: structure is grey, and colour
        # is spent only on meaning — which here is the stall above, and nothing
        # else. A spinner that is permanently cyan has spent an accent on the
        # fact that something is running, which is the least surprising thing
        # on the screen.
        return (ansi.paint(f"  {frame} ", "grey")
                + ansi.paint(f"{label}…", "bold")
                + ansi.paint(f"  ({' · '.join(bits)})", "dim"))

    async def _run(self) -> None:
        try:
            while True:
                if not self._paused:
                    ansi.transient(self.render())
                    self._visible = True
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken spinner must not end a turn
            self.clear()
