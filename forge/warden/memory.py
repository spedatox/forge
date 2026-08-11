"""Seam 2's sibling — reaching the owner's memory, which lives in Mark VI.

The Forge does not own the owner's memory and must not start. Mark VI holds it
in a database with a schema, a revision trail, per-document ownership and a
custodian that repairs it; a peer keeping its own copy is the "memory that
quietly forks" that `agents/owner_memory.py` argues against at length, and the
argument has not changed. So this is a *channel*, not a store: the tool asks,
Mark VI answers, and nothing is cached.

**Why it had to exist at all.** Mark VI's memory redesign moved the injected
block from "here is everything" to "here is a directory listing and four
preloaded files — read the rest with the `memory` tool". Every in-process agent
has that tool. The peer did not, so the block arriving over the socket was
instructing an agent to call something it had never been given: it could not
open a project file, could not look up a person, and could not write down a
single thing it learned. The block still says to, which is worse than silence —
an agent told to use a tool it does not have does not conclude the tool is
missing, it concludes it has already remembered.

**Nothing here decides what memory means.** No path rules, no schema, no
routing: those are Mark VI's, they are enforced server-side, and duplicating
them would give the owner two answers to the same question the first time one
side was edited. This carries a command and returns whatever came back.

The failure direction is fixed the way the oracle's is, and to the same end. An
unreachable backend returns a plain error the model can act on. What it must
never do is succeed quietly — an agent that believes it filed something, and
did not, has lost the fact AND the knowledge that it lost it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger("forge.warden")

DEFAULT_MEMORY_TIMEOUT_S = 30.0
"""How long a memory call waits for the backend.

Generous for what is one database round trip, and deliberately far below the
300s idle ceiling Mark VI's proxy applies to the whole turn: a memory read that
has silently died must fail long before it can take the conversation with it."""


@dataclass(frozen=True)
class MemoryReply:
    """What came back. `ok=False` carries a message meant for the model."""
    ok: bool
    text: str = ""


@runtime_checkable
class MemoryChannel(Protocol):
    async def command(self, payload: dict[str, Any]) -> MemoryReply:
        """Run one memory command and return its result. Must always return."""
        ...


class RemoteMemory:
    """Mark VI's memory, over the peer socket it is already talking on.

    The same park-and-correlate shape as `ChannelOracle`, and for the same
    reason: the wait happens inside one tool dispatch, so interrupt boundaries,
    transcript shape and batch semantics all hold — from the loop's point of
    view one tool simply took a moment.

    A second socket to the same backend was the alternative and would have been
    worse in every direction: another thing to authenticate, another thing to
    reconnect, and a memory write that could succeed while the conversation it
    belonged to had already lost its connection.
    """

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 timeout_s: float = DEFAULT_MEMORY_TIMEOUT_S,
                 chat_id: str | None = None) -> None:
        self._send = send
        self._timeout = timeout_s
        self._chat_id = chat_id
        self._pending: dict[str, asyncio.Future[MemoryReply]] = {}

    def scoped(self, chat_id: str) -> RemoteMemory:
        """A view of this channel labelled with the turn it belongs to.

        Shares `_pending`, so a reply lands wherever it was parked no matter
        which view sent it. Mark VI needs the chat_id to know whose memory this
        is; the peer runs several conversations over one socket and the frame is
        the only place that association can be carried."""
        clone = RemoteMemory(self._send, self._timeout, chat_id)
        clone._pending = self._pending
        return clone

    async def command(self, payload: dict[str, Any]) -> MemoryReply:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[MemoryReply] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = {"type": "memory_request", "request_id": request_id, **payload}
        if self._chat_id:
            frame["chat_id"] = self._chat_id
        try:
            await self._send(frame)
        except Exception as e:  # noqa: BLE001 — an unsendable request is a failed one
            logger.warning("memory_request_send_failed", extra={"error": repr(e)})
            self._pending.pop(request_id, None)
            return MemoryReply(False, "The memory backend could not be reached: the "
                                      "connection to Mark VI is down.")
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.info("memory_request_timed_out", extra={"request_id": request_id})
            return MemoryReply(False, f"The memory backend did not answer within "
                                      f"{self._timeout:.0f}s.")
        except asyncio.CancelledError:
            raise                          # an interrupt is not an answer
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, reply: MemoryReply) -> bool:
        """Deliver an answer. False if nothing was waiting — a late reply to a
        call that already timed out is not an error."""
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(reply)
        return True

    def abandon_all(self, note: str = "the connection to Mark VI closed") -> None:
        """Fail every parked call. Called on socket teardown: losing the channel
        is a reason to say so, never a reason to hang — and never, on a write, a
        reason to let the model believe the fact was filed."""
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(MemoryReply(False, note))
            self._pending.pop(request_id, None)
