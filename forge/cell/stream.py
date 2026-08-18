"""Reading a command's output while it is still running.

Both backends need the same three things and used to have none of them:

**Output that survives the deadline.** `communicate()` returns its pair only on
success. Cancelled at a timeout it yields nothing, so every killed command
reported an empty stdout — and the output before a wedge is the entire
diagnosis. A hanging `pytest` names the test it stopped on; a hanging build
names the file.

**Output before the end.** A five-minute build that shows nothing for five
minutes is indistinguishable, from the outside, from a hang. Streaming is what
turns waiting into watching, and it is the precondition for letting an operator
decide when a command has gone on too long — an unbounded command you cannot see
is not a choice, it is a freeze.

**A bound while reading, not after.** Draining into a plain buffer reintroduces
exactly the growth `max_output_bytes` exists to prevent, and worse: it
accumulates for as long as the command is allowed to run rather than being
capped by a finished result. So the cap is applied per chunk, and it keeps both
ends, because the answer is at the end.
"""
from __future__ import annotations

import asyncio

CHUNK = 65_536

# How long cleanup may take before it is itself abandoned. The kill has already
# been sent; this only covers reaping the corpse.
REAP_GRACE_S = 5

# Room left for the omission marker, so rendered text still fits under the
# policy's byte cap and `Cell._cap` downstream stays a no-op. Without it the two
# bound the same text in sequence and the result carries a nested marker.
_MARKER_RESERVE = 200


class Retained:
    """A bounded accumulator that keeps the head and a rolling tail.

    The reader never stops consuming, even once full: a pipe nobody drains
    fills, and a full pipe blocks the writer. A command blocked on its own
    output is a hang the harness caused.
    """

    __slots__ = ("_limit", "_head", "_tail", "_omitted")

    def __init__(self, limit: int) -> None:
        self._limit = max(2, limit - _MARKER_RESERVE)
        self._head = bytearray()
        self._tail = bytearray()
        self._omitted = 0

    def feed(self, chunk: bytes) -> None:
        half = self._limit // 2
        room = half - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail += chunk
        excess = len(self._tail) - half
        if excess > 0:
            del self._tail[:excess]
            self._omitted += excess

    def raw(self) -> bytes:
        """Bytes, with the omission marker inline. For a caller that decodes."""
        if not self._omitted:
            return bytes(self._head + self._tail)
        marker = (f"\n…[{self._omitted} bytes omitted from the middle of this "
                  f"stream]…\n").encode("utf-8")
        return bytes(self._head) + marker + bytes(self._tail)

    def text(self) -> str:
        return self.raw().decode("utf-8", "replace")


async def drain(stream, into: Retained, name: str = "", on_output=None) -> None:
    """Consume one pipe to EOF. Cancellation leaves `into` holding what it read.

    A read that raises — a closed transport, a decoding fault in the loop's own
    machinery — ends this reader without ending the other one or the command.
    Partial output is the point; an exception here would discard exactly what
    the caller came for.

    `on_output` sees each chunk as it lands. It is wrapped and dropped on first
    fault: a renderer that throws must not kill the reader, because the reader
    is also what keeps the pipe from filling, and one bad chunk would otherwise
    become a bad chunk per read for the rest of the command.
    """
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(CHUNK)
            if not chunk:
                return
            into.feed(chunk)
            if on_output is not None:
                try:
                    on_output(name, chunk.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 — see docstring
                    on_output = None
    except (asyncio.LimitOverrunError, ValueError, OSError):
        return
