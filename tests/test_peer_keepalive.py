"""A busy stream must not read as a dead one.

The regression the operator saw as "Centurion stopped responding mid-task (no
events for 300s)": Mark VI's proxy resets its 300s idle clock only when a
`chat_event` reaches it, and the run loop emits one only when the model streams
text or a tool starts or returns. A single long command — a cold `wpscan`
database update, a wide `nuclei` sweep — makes none of those for its whole
duration, so the stream falls silent and Mark VI kills a turn that is working
fine. `_chat_keepalive` backfills an empty `chunk` while, and only while, real
activity is absent, keeping the socket alive without putting anything in front
of the operator.

The peer is built via `__new__` so the test drives the coroutine alone, without
`load_extensions`, a socket, or a backend — the method touches only `cfg` and
`_send`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import forge.gate.peer as peer_mod
from forge.gate.peer import ForgePeer
from forge.gate.protocol import JobEvent
from forge.warden.state import StopReason


def _bare_peer(send):
    peer = ForgePeer.__new__(ForgePeer)
    peer.cfg = SimpleNamespace(agent_id="centurion", name="Centurion")
    peer._send = send
    return peer


def test_a_silent_stream_gets_an_empty_keepalive_chunk(monkeypatch):
    """No real frames for a whole cycle → the proxy clock is reset for it."""
    monkeypatch.setattr(peer_mod, "_CHAT_KEEPALIVE_S", 0.01)
    sends: list[dict] = []

    async def send(frame):
        sends.append(frame)

    peer = _bare_peer(send)

    async def drive():
        # last_activity frozen in the past → the stream is always "quiet".
        past = asyncio.get_running_loop().time() - 1000.0
        task = asyncio.create_task(peer._chat_keepalive("c1", lambda: past))
        await asyncio.sleep(0.05)  # several cycles
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert sends, "a silent stream must be nudged before the 300s ceiling"
    ev = sends[0]
    assert ev["type"] == "chat_event"
    assert ev["chat_id"] == "c1"
    assert ev["agent_id"] == "centurion"
    # Empty payload: a type the proxy resets on, invisible to the client.
    assert ev["event"] == {"type": "chunk", "data": ""}


def test_an_active_stream_is_never_nudged(monkeypatch):
    """Real frames keep resetting `last_activity`, so no keepalive is sent —
    the nudge is for silence only, never noise on top of a streaming answer."""
    monkeypatch.setattr(peer_mod, "_CHAT_KEEPALIVE_S", 0.01)
    sends: list[dict] = []

    async def send(frame):
        sends.append(frame)

    peer = _bare_peer(send)

    async def drive():
        loop = asyncio.get_running_loop()
        # last_activity is always "now" → never quiet past the threshold.
        task = asyncio.create_task(peer._chat_keepalive("c1", loop.time))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert sends == [], "an active stream must not be nudged"


def test_keepalive_gives_up_quietly_when_the_socket_is_gone(monkeypatch):
    """`_send` raising means the connection is gone; the loop returns instead of
    spinning, and the run unwinds through its own path."""
    monkeypatch.setattr(peer_mod, "_CHAT_KEEPALIVE_S", 0.01)
    calls: list[dict] = []

    async def boom(frame):
        calls.append(frame)
        raise ConnectionError("socket gone")

    peer = _bare_peer(boom)

    async def drive():
        past = asyncio.get_running_loop().time() - 1000.0
        # Must return on its own — a timeout here means it kept spinning.
        await asyncio.wait_for(peer._chat_keepalive("c1", lambda: past), timeout=1.0)

    asyncio.run(drive())

    assert len(calls) == 1, "it should try once, then stop on the dead socket"


def _handler_peer(send):
    """A peer wired just enough to run `_handle_chat` with a faked `run_job`."""
    peer = _bare_peer(send)
    peer._chats = {}
    peer.settings = None
    peer.registry = None
    peer._oracle = None
    peer.allowlist = None
    peer._memory = SimpleNamespace(scoped=lambda chat_id: None)
    peer.extensions = SimpleNamespace(
        tool_providers=lambda: [], fragments=None, hooks=None, bus=None)
    peer._cellpool = peer_mod.CellPool()
    peer._inboxes = {}
    return peer


def test_non_forwarded_events_do_not_pass_as_activity(monkeypatch):
    """The bug the reorder fixes: only a frame that REACHES Mark VI resets its
    idle clock, so `emit` must timestamp on the send, not on every JobEvent. A
    run that produces only internal (non-forwarded) events sends Mark VI nothing
    — the keepalive must still fire through that silence, not be fooled into
    thinking the stream is busy."""
    monkeypatch.setattr(peer_mod, "_CHAT_KEEPALIVE_S", 0.01)
    sends: list[dict] = []

    async def send(frame):
        sends.append(frame)

    async def fake_run_job(request, *, emit, **kw):
        # Several internal events, none in _CHAT_FORWARD: Mark VI gets nothing.
        for _ in range(3):
            await emit(JobEvent(job_id=request.job_id, type="started", data=None))
            await asyncio.sleep(0.03)   # a few keepalive cycles
        await emit(JobEvent(job_id=request.job_id, type="done", data="ok"))
        return SimpleNamespace(reason=StopReason.COMPLETED, final_text="ok", error=None)

    monkeypatch.setattr(peer_mod, "run_job", fake_run_job)
    peer = _handler_peer(send)

    asyncio.run(peer._handle_chat({"chat_id": "c1", "history": []}))

    events = [f["event"] for f in sends if f.get("type") == "chat_event"]
    keepalives = [e for e in events if e == {"type": "chunk", "data": ""}]
    assert keepalives, "keepalive must fire while only non-forwarded events flow"
    # The internal `started` events were dropped; the terminal `done` got through.
    assert {"type": "done", "data": "ok"} in events
    assert not any(e.get("type") == "started" for e in events)


def test_forwarded_output_keeps_the_keepalive_quiet(monkeypatch):
    """The other side: a run that actually streams to Mark VI resets the clock
    every cycle, so the keepalive stays silent — no empty chunks on top of a
    live answer."""
    # Frames arrive FASTER than the keepalive threshold (as real streaming does
    # against the real 90s), so the clock is always fresh and no nudge is due.
    monkeypatch.setattr(peer_mod, "_CHAT_KEEPALIVE_S", 0.05)
    sends: list[dict] = []

    async def send(frame):
        sends.append(frame)

    async def fake_run_job(request, *, emit, **kw):
        for i in range(15):
            await emit(JobEvent(job_id=request.job_id, type="chunk", data=f"t{i}"))
            await asyncio.sleep(0.01)   # well under the 0.05 threshold
        await emit(JobEvent(job_id=request.job_id, type="done", data="ok"))
        return SimpleNamespace(reason=StopReason.COMPLETED, final_text="ok", error=None)

    monkeypatch.setattr(peer_mod, "run_job", fake_run_job)
    peer = _handler_peer(send)

    asyncio.run(peer._handle_chat({"chat_id": "c1", "history": []}))

    events = [f["event"] for f in sends if f.get("type") == "chat_event"]
    empty = [e for e in events if e == {"type": "chunk", "data": ""}]
    assert empty == [], "a streaming answer must not be padded with keepalives"
