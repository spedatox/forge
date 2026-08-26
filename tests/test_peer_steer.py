"""Steering a running peer turn: a mid-turn message reaches the engine's inbox.

The engine has always been able to fold operator input claimed at a safe loop
boundary (forge/warden/inbox.py); the TUI wired it, the peer did not, on the old
assumption that a dispatched job has nobody at a keyboard. The /bg + Telegram
path broke that assumption — the owner IS reachable — so the peer now gives each
chat turn an Inbox and a `chat_steer` frame pushes into it. These pin the wiring:
a steer reaches the right turn's inbox, a steer for a turn that has ended is
dropped rather than erroring, and _handle_chat hands the inbox to run_job.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import forge.gate.peer as peer_mod
from forge.gate.peer import ForgePeer
from forge.warden.inbox import Inbox
from forge.warden.state import StopReason


def _peer() -> ForgePeer:
    peer = ForgePeer.__new__(ForgePeer)
    peer.cfg = SimpleNamespace(agent_id="optimus", name="Optimus")
    peer._chats = {}
    peer._inboxes = {}
    return peer


def test_chat_steer_pushes_into_the_running_turns_inbox():
    peer = _peer()
    box = Inbox()
    peer._inboxes["c1"] = box

    peer._dispatch({"type": "chat_steer", "chat_id": "c1", "text": "also update the README"})

    assert box.claim() == ["also update the README"]


def test_chat_steer_for_an_unknown_chat_is_dropped_not_raised():
    peer = _peer()
    # No inbox registered for c9 (turn already ended). Must be a no-op.
    peer._dispatch({"type": "chat_steer", "chat_id": "c9", "text": "late"})
    assert "c9" not in peer._inboxes


def test_blank_steer_is_not_queued():
    peer = _peer()
    box = Inbox()
    peer._inboxes["c1"] = box
    peer._dispatch({"type": "chat_steer", "chat_id": "c1", "text": "   "})
    assert box.claim() == []


def test_handle_chat_gives_run_job_an_inbox(monkeypatch):
    """The inbox reaches run_job so the engine can claim from it, and it is the
    same object a concurrent chat_steer pushes into."""
    peer = _peer()
    peer.settings = None
    peer.registry = None
    peer._oracle = None
    peer.allowlist = None
    peer._memory = SimpleNamespace(scoped=lambda chat_id: None)
    peer.extensions = SimpleNamespace(
        tool_providers=lambda: [], fragments=None, hooks=None, bus=None)
    peer._cellpool = peer_mod.CellPool()

    async def send(frame):
        return None
    peer._send = send

    seen = {}

    async def fake_run_job(request, *, emit, inbox=None, **kw):
        seen["inbox"] = inbox
        # While the turn runs, a steer arrives for this chat.
        peer._dispatch({"type": "chat_steer", "chat_id": "c1", "text": "steered"})
        await emit(peer_mod.JobEvent(job_id=request.job_id, type="done", data="ok"))
        return SimpleNamespace(reason=StopReason.COMPLETED, final_text="ok", error=None)

    monkeypatch.setattr(peer_mod, "run_job", fake_run_job)
    asyncio.run(peer._handle_chat({"chat_id": "c1", "history": []}))

    assert isinstance(seen["inbox"], Inbox)
    # The steer that arrived mid-run landed in the very inbox run_job was given.
    assert seen["inbox"].claim() == ["steered"]
    # And it was cleaned up when the turn ended.
    assert "c1" not in peer._inboxes


