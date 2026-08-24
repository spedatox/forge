"""A per-conversation Cell pool.

The default posture is a fresh, throwaway Cell per job (§9.1): one job, one
sandbox, gone when it ends. That is exactly right for a dispatch. On the CHAT
path it is not — there, "job" means one turn, so every turn and every retry
after a timeout rebuilds the container from the base image. The agent re-fetches
nuclei's templates, re-updates wpscan's database, re-installs whatever it
apt-got, and re-runs the scan whose output lived only in the container that was
just torn down. To the operator that reads as the conversation resetting itself.

This pool keeps ONE live Cell per conversation, keyed by chat id, so a
conversation's installed tooling and caches survive across its turns. The
isolation that §9.1 buys is kept where it matters — BETWEEN conversations:
distinct keys never share a Cell, and a Cell is reclaimed when its conversation
goes idle, when the pool is over its cap, or when the peer stops.

Two safeguards keep reuse honest:

  * The construction *signature* (image, backend, workspace, resource policy) is
    stored with each Cell. A turn that arrives with a different signature — a new
    cwd, a policy change — does not silently inherit the old container; the pool
    evicts it and builds one that matches.
  * A concurrent second turn on the same key (rare — Mark VI serialises a chat)
    is handed a throwaway Cell instead of the live one, so two turns can never
    interleave commands inside a single container.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Hashable

from forge.cell.base import Cell

logger = logging.getLogger("forge.gate.cellpool")

CellSig = tuple
"""Everything about a Cell's construction that reuse must match. Two turns with
equal signatures may share a container; a difference forces a rebuild."""

CellBuilder = Callable[[], Awaitable[Cell]]

_DEFAULT_MAX_CELLS = 8
_DEFAULT_IDLE_TTL_S = 1800.0     # 30 min: a conversation nobody has touched
_ALIVE_PROBE_S = 5.0


@dataclass
class _Entry:
    cell: Cell
    sig: CellSig
    in_use: bool = False
    last_used: float = 0.0


class CellPool:
    """Live Cells kept alive across the turns of a conversation.

    `acquire` hands back `(cell, pooled)`: when `pooled` is True the caller must
    return it with `release` (which keeps it alive for the next turn); when
    False it is a throwaway the caller still returns with `release`, and the pool
    simply closes it. Closing is always the pool's job, so a caller never has to
    know which kind it holds."""

    def __init__(self, *, max_cells: int = _DEFAULT_MAX_CELLS,
                 idle_ttl_s: float = _DEFAULT_IDLE_TTL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[Hashable, _Entry] = {}
        self._gate = asyncio.Lock()      # guards _entries; held only for fast bookkeeping
        self._max = max(1, max_cells)
        self._ttl = idle_ttl_s
        self._clock = clock
        self._closed = False

    async def acquire(self, key: Hashable, sig: CellSig,
                      build: CellBuilder) -> tuple[Cell, bool]:
        """Lease a Cell for `key`. Reuses the conversation's live Cell when one
        exists, matches `sig`, is free, and answers a liveness probe; otherwise
        builds a fresh one (throwaway if the live one is busy, pooled if not)."""
        # Phase 1 — decide under the gate, collecting anything to close later.
        stale: list[Cell] = []
        async with self._gate:
            self._pop_idle_locked(stale)
            entry = self._entries.get(key)
            if entry is not None and not entry.in_use and entry.sig == sig:
                entry.in_use = True
                entry.last_used = self._clock()
                plan, planned_cell = "probe", entry.cell
            elif entry is not None and entry.in_use:
                plan, planned_cell = "ephemeral", None
            else:
                if entry is not None:            # free but signature changed → drop
                    stale.append(entry.cell)
                    self._entries.pop(key, None)
                plan, planned_cell = "build", None

        # Phase 2 — the slow parts (close / probe / build) run off the gate.
        for dead in stale:
            await self._safe_close(dead)

        if plan == "probe":
            assert planned_cell is not None
            if await self._alive(planned_cell):
                return planned_cell, True
            # The container died out from under us — drop it and build fresh.
            async with self._gate:
                cur = self._entries.get(key)
                if cur is not None and cur.cell is planned_cell:
                    self._entries.pop(key, None)
            await self._safe_close(planned_cell)
            plan = "build"

        if plan == "ephemeral":
            logger.info("cellpool_ephemeral", extra={"key": _short(key)})
            return await build(), False

        # plan == "build": a pooled Cell for this conversation.
        cell = await build()
        replaced: list[Cell] = []
        async with self._gate:
            prev = self._entries.get(key)
            if prev is not None and not prev.in_use:
                # A concurrent acquire built one too; keep this, close the loser.
                replaced.append(prev.cell)
            self._entries[key] = _Entry(cell=cell, sig=sig, in_use=True,
                                        last_used=self._clock())
            self._pop_overflow_locked(protect=key, out=replaced)
        for dead in replaced:
            await self._safe_close(dead)
        return cell, True

    async def release(self, key: Hashable, cell: Cell, pooled: bool) -> None:
        """Return a leased Cell. A throwaway is closed; a pooled one is parked
        free for the conversation's next turn."""
        # A worktree narrowing is a per-turn concern: never let one turn's
        # `enter_worktree` leak into the next turn that reuses this Cell.
        try:
            cell.leave_subpath()
        except Exception:  # noqa: BLE001 — a backend without subpaths is fine
            pass
        if not pooled:
            await self._safe_close(cell)
            return
        async with self._gate:
            entry = self._entries.get(key)
            if entry is not None and entry.cell is cell:
                entry.in_use = False
                entry.last_used = self._clock()
                return
        # The entry was replaced or evicted while this turn ran — the Cell is an
        # orphan now, and closing it is the only way it gets reclaimed.
        await self._safe_close(cell)

    async def close_all(self) -> None:
        """Tear down every pooled Cell. Called when the peer stops."""
        async with self._gate:
            self._closed = True
            cells = [e.cell for e in self._entries.values()]
            self._entries.clear()
        for cell in cells:
            await self._safe_close(cell)

    # ── internals ────────────────────────────────────────────────────────────

    def _pop_idle_locked(self, out: list[Cell]) -> None:
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if not entry.in_use and (now - entry.last_used) > self._ttl:
                out.append(entry.cell)
                self._entries.pop(key, None)

    def _pop_overflow_locked(self, *, protect: Hashable, out: list[Cell]) -> None:
        while len(self._entries) > self._max:
            victim = min(
                (k for k, e in self._entries.items()
                 if not e.in_use and k != protect),
                key=lambda k: self._entries[k].last_used,
                default=None,
            )
            if victim is None:      # everything else is busy — nothing to evict
                return
            out.append(self._entries.pop(victim).cell)

    async def _alive(self, cell: Cell) -> bool:
        try:
            res = await cell.run("true", timeout=int(_ALIVE_PROBE_S))
            return res.exit_code == 0 and not res.timed_out
        except Exception:  # noqa: BLE001 — any failure means "not usable"
            return False

    async def _safe_close(self, cell: Cell) -> None:
        try:
            await cell.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.warning("cellpool_close_failed", exc_info=True)


def _short(key: Hashable) -> str:
    s = str(key)
    return s if len(s) <= 12 else s[:12]
