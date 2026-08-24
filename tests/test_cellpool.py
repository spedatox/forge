"""One Cell per conversation, kept alive across its turns.

The reset the operator saw on retry: a fresh throwaway container per turn means a
long turn that timed out threw away the tools and caches it had installed, and
the retry started from the base image again. `CellPool` keeps a conversation's
Cell alive between turns and retries, while never sharing one between two
conversations or letting a dead container be reused.
"""
from __future__ import annotations

import asyncio

from forge.cell.base import CommandResult
from forge.gate.cellpool import CellPool


class FakeCell:
    """Enough of the Cell surface for the pool: a liveness `run`, a `close`
    counter, and a settable `alive` flag to simulate a dead container."""

    _n = 0

    def __init__(self):
        FakeCell._n += 1
        self.id = FakeCell._n
        self.alive = True
        self.closed = 0
        self._subpath = ""

    async def run(self, command, timeout=None, env=None, on_output=None):
        code = 0 if self.alive else 1
        return CommandResult(stdout="", stderr="", exit_code=code)

    def leave_subpath(self):
        self._subpath = ""

    async def close(self):
        self.closed += 1
        self.alive = False


def _builder(made: list):
    async def build():
        c = FakeCell()
        made.append(c)
        return c
    return build


SIG = ("centurion", "docker", "img", "/ws", True, 2.0, 2048, 256, True, ())


def test_a_conversation_reuses_one_cell_across_turns():
    """Two turns of the same chat get the SAME live container back."""
    made: list = []
    pool = CellPool()

    async def go():
        c1, pooled1 = await pool.acquire("chatA", SIG, _builder(made))
        assert pooled1
        await pool.release("chatA", c1, pooled1)
        c2, pooled2 = await pool.acquire("chatA", SIG, _builder(made))
        await pool.release("chatA", c2, pooled2)
        return c1, c2

    c1, c2 = asyncio.run(go())
    assert c1 is c2, "the second turn must reuse the first turn's Cell"
    assert len(made) == 1, "only one container should ever have been built"
    assert c1.closed == 0, "a reused Cell must not be torn down between turns"


def test_distinct_conversations_never_share_a_cell():
    made: list = []
    pool = CellPool()

    async def go():
        a, pa = await pool.acquire("chatA", SIG, _builder(made))
        b, pb = await pool.acquire("chatB", SIG, _builder(made))
        await pool.release("chatA", a, pa)
        await pool.release("chatB", b, pb)
        return a, b

    a, b = asyncio.run(go())
    assert a is not b
    assert len(made) == 2


def test_a_changed_signature_forces_a_rebuild():
    """A new cwd or resource policy must not inherit the old container."""
    made: list = []
    pool = CellPool()
    other_sig = ("centurion", "docker", "img", "/different-ws",
                 True, 2.0, 2048, 256, True, ())

    async def go():
        c1, p1 = await pool.acquire("chatA", SIG, _builder(made))
        await pool.release("chatA", c1, p1)
        c2, p2 = await pool.acquire("chatA", other_sig, _builder(made))
        await pool.release("chatA", c2, p2)
        return c1, c2

    c1, c2 = asyncio.run(go())
    assert c1 is not c2, "a different signature must build a fresh Cell"
    assert c1.closed == 1, "the stale Cell must be closed, not left running"


def test_a_dead_container_is_replaced_on_reuse():
    """If the pooled container died between turns, the probe catches it and the
    next turn gets a fresh one instead of a container every command would fail
    against."""
    made: list = []
    pool = CellPool()

    async def go():
        c1, p1 = await pool.acquire("chatA", SIG, _builder(made))
        await pool.release("chatA", c1, p1)
        c1.alive = False              # container killed out from under the pool
        c2, p2 = await pool.acquire("chatA", SIG, _builder(made))
        await pool.release("chatA", c2, p2)
        return c1, c2

    c1, c2 = asyncio.run(go())
    assert c2 is not c1
    assert len(made) == 2


def test_a_concurrent_second_turn_gets_a_throwaway():
    """While one turn holds the conversation's Cell, an overlapping second turn
    on the same chat is handed an unpooled Cell — never the same container — so
    the two can't interleave commands. The throwaway is closed on release."""
    made: list = []
    pool = CellPool()

    async def go():
        c1, p1 = await pool.acquire("chatA", SIG, _builder(made))    # held
        c2, p2 = await pool.acquire("chatA", SIG, _builder(made))    # overlaps
        assert p1 is True and p2 is False
        assert c1 is not c2
        await pool.release("chatA", c2, p2)
        assert c2.closed == 1, "the throwaway must be closed on release"
        await pool.release("chatA", c1, p1)
        return c1

    c1 = asyncio.run(go())
    assert c1.closed == 0, "the pooled Cell stays alive after the overlap clears"


def test_idle_conversations_are_reaped():
    made: list = []
    t = {"now": 1000.0}
    pool = CellPool(idle_ttl_s=100.0, clock=lambda: t["now"])

    async def go():
        a, pa = await pool.acquire("chatA", SIG, _builder(made))
        await pool.release("chatA", a, pa)
        t["now"] += 1000.0            # chatA now idle well past its TTL
        b, pb = await pool.acquire("chatB", SIG, _builder(made))  # triggers reap
        await pool.release("chatB", b, pb)
        return a

    a = asyncio.run(go())
    assert a.closed == 1, "an idle conversation's Cell must be reclaimed"


def test_the_cap_evicts_the_least_recently_used():
    made: list = []
    t = {"now": 0.0}
    pool = CellPool(max_cells=2, idle_ttl_s=1e9, clock=lambda: t["now"])

    async def go():
        for key in ("A", "B", "C"):
            t["now"] += 1.0
            c, p = await pool.acquire(key, SIG, _builder(made))
            await pool.release(key, c, p)
        return made

    made = asyncio.run(go())
    # A was least-recently-used when C arrived over the cap of 2.
    assert made[0].closed == 1, "the LRU conversation must be evicted"
    assert made[1].closed == 0 and made[2].closed == 0


def test_close_all_tears_down_every_pooled_cell():
    made: list = []
    pool = CellPool()

    async def go():
        a, pa = await pool.acquire("chatA", SIG, _builder(made))
        b, pb = await pool.acquire("chatB", SIG, _builder(made))
        await pool.release("chatA", a, pa)
        await pool.release("chatB", b, pb)
        await pool.close_all()

    asyncio.run(go())
    assert all(c.closed == 1 for c in made), "every pooled Cell must be closed"


def test_release_clears_a_leaked_worktree_narrowing():
    """A turn that entered a worktree and did not leave it must not hand the
    next turn a Cell still narrowed to that subpath."""
    made: list = []
    pool = CellPool()

    async def go():
        c, p = await pool.acquire("chatA", SIG, _builder(made))
        c._subpath = "some/worktree"
        await pool.release("chatA", c, p)
        return c

    c = asyncio.run(go())
    assert c._subpath == "", "release must reset the per-turn subpath"
