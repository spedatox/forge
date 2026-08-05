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

FRAMES = ("✻", "✼", "✽", "✻", "✦", "✧")
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

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            return
        self._started = time.monotonic()
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

    # ── what it reports ──────────────────────────────────────────────────────

    def add_chars(self, n: int) -> None:
        """Streamed response characters, used as a rough token proxy.

        Rough on purpose: the exact count arrives with the usage report at the
        END of the turn, which is precisely when it stops being useful. ~4 chars
        per token is close enough to answer "is this getting expensive".
        """
        self._chars += max(0, n)

    def set_status(self, status: str) -> None:
        """A short note replacing the verb — a running tool, a compaction, a
        retry. What the harness is doing beats a generic verb every time."""
        self._status = status.strip()

    # ── drawing ──────────────────────────────────────────────────────────────

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    def render(self, now: float | None = None) -> str:
        t = self.elapsed_s if now is None else now
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

        return (ansi.paint(f"  {frame} ", "cyan")
                + ansi.paint(f"{label}…", "bold")
                + ansi.paint(f"  ({' · '.join(bits)})", "dim"))

    async def _run(self) -> None:
        try:
            while True:
                ansi.transient(self.render())
                self._visible = True
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken spinner must not end a turn
            self.clear()
