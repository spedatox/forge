"""A dispatched job streams its progress, instead of being a black box.

`task_dispatch` is fire-and-await on the wire — one frame out, one `task_result`
back — and `_handle_task`'s sink used to discard every event in between. A
backgrounded job therefore showed the owner a "running" row for minutes with
nothing behind it and no way to tell real work from a wedge.

The peer now also emits `task_event` frames carrying the same event vocabulary a
chat streams. These pin that wiring: the forwardable events go out correlated by
task_id, the internal ones do not, the single `task_result` still lands, and a
send that fails mid-run never costs the job its result.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import forge.gate.peer as peer_mod
from forge.gate.peer import ForgePeer
from forge.gate.protocol import JobEvent
from forge.warden.state import StopReason


def _peer(sent: list, *, send_fails: bool = False) -> ForgePeer:
    peer = ForgePeer.__new__(ForgePeer)
    peer.cfg = SimpleNamespace(agent_id="optimus", name="Optimus", tool_names=[])
    peer.settings = SimpleNamespace()
    peer.registry = None
    peer._chats = {}
    peer._inboxes = {}
    peer._cellpool = None
    peer.allowlist = None
    peer._oracle = None
    peer._memory = None
    peer.extensions = SimpleNamespace(
        tool_providers=lambda: [], fragments=[], hooks=[], bus=None,
    )

    async def _send(frame):
        if send_fails:
            raise ConnectionError("socket gone")
        sent.append(frame)

    peer._send = _send
    return peer


def _run_task(peer, events, *, reason=StopReason.COMPLETED, final="done here"):
    """Drive _handle_task with a run_job stub that emits `events`."""
    async def fake_run_job(request, **kw):
        for ev in events:
            await kw["emit"](ev)
        return SimpleNamespace(reason=reason, final_text=final, error=None)

    async def scenario():
        import forge.gate.peer as m
        original = m.run_job
        m.run_job = fake_run_job
        try:
            await peer._handle_task(
                {"type": "task_dispatch", "task_id": "t-42", "from": "igor",
                 "task": "refactor the parser", "cwd": None}
            )
        finally:
            m.run_job = original

    asyncio.run(scenario())


def test_a_dispatched_job_streams_its_progress():
    sent: list = []
    peer = _peer(sent)
    _run_task(peer, [
        JobEvent(job_id="t-42", type="tool", data={"name": "read_file"}),
        JobEvent(job_id="t-42", type="chunk", data="looking at the parser"),
        JobEvent(job_id="t-42", type="tool_result", data={"tool_use_id": "x", "content": "ok"}),
    ])

    progress = [f for f in sent if f["type"] == "task_event"]
    assert len(progress) == 3, f"expected every forwardable event to stream; got {sent}"
    assert all(f["task_id"] == "t-42" for f in progress), (
        "each frame must carry the task_id, or the backend cannot correlate it "
        "to the tray row the client is attached to"
    )
    assert [f["event"]["type"] for f in progress] == ["tool", "chunk", "tool_result"]


def test_the_single_task_result_still_lands():
    """The streaming is additive — the fire-and-await contract is unchanged."""
    sent: list = []
    peer = _peer(sent)
    _run_task(peer, [JobEvent(job_id="t-42", type="chunk", data="working")])

    results = [f for f in sent if f["type"] == "task_result"]
    assert len(results) == 1, f"exactly one task_result, got {results}"
    assert results[0]["task_id"] == "t-42"
    assert results[0]["status"] == "ok"
    assert results[0]["result"] == "done here"


def test_internal_events_are_not_streamed():
    """Only the vocabulary the backend maps goes out; the rest stays local."""
    sent: list = []
    peer = _peer(sent)
    _run_task(peer, [
        JobEvent(job_id="t-42", type="chunk", data="visible"),
        JobEvent(job_id="t-42", type="iteration", data={"n": 3}),
        JobEvent(job_id="t-42", type="debug", data="internal"),
    ])

    streamed = [f["event"]["type"] for f in sent if f["type"] == "task_event"]
    assert streamed == ["chunk"], f"internal events must not stream; got {streamed}"


def test_a_failed_progress_send_never_costs_the_job_its_result():
    """Losing the socket mid-run must not turn a finished job into a crash.

    The result frame is attempted on the same dead socket and fails too, but the
    point is that _handle_task returns cleanly rather than propagating — a
    progress frame is never worth the job.
    """
    sent: list = []
    peer = _peer(sent, send_fails=True)

    _run_task(peer, [JobEvent(job_id="t-42", type="chunk", data="working")])  # must not raise

    assert peer._chats == {}, "the job must still be unregistered on the way out"
