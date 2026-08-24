"""remember_about_owner — the tool surface over owner_memory.queue_observation
and flush_pending, covered on their own in test_owner_memory.py. This file is
about what the TOOL does with a live vs. absent channel: queue always, and
flush immediately when one is already open rather than making it wait for a
reconnect that may be a long way off.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tools.owner_notes import RememberAboutOwner, RememberAboutOwnerArgs
from forge.warden.memory import MemoryReply
from forge.warden.tool import ToolContext


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    return tmp_path


class _Channel:
    def __init__(self, reply: MemoryReply | None = None) -> None:
        self.reply = reply or MemoryReply(True, "recorded")
        self.sent: list[dict] = []

    async def command(self, payload: dict) -> MemoryReply:
        self.sent.append(payload)
        return self.reply


def _ctx(memory=None) -> ToolContext:
    return ToolContext(agent_id="optimus", cell=None, graph=None, files=None,
                       permissions=None, network_allowed=False, memory=memory)


def _call(content: str, domain: str = "state", channel=None):
    tool = RememberAboutOwner()
    args = RememberAboutOwnerArgs(content=content, domain=domain)
    return asyncio.run(tool.call(args, _ctx(channel)))


# ── No channel: queued, no network required ──────────────────────────────────

def test_it_works_with_no_channel_at_all():
    from forge.agents import owner_memory

    result = _call("Prefers dark mode.")

    assert result.is_error is False
    assert "next time this peer connects" in result.content
    assert "dark mode" in owner_memory.pending_path().read_text(encoding="utf-8")


# ── A live channel: flushed immediately, not left for the next reconnect ────

def test_a_connected_peer_sends_it_right_away():
    from forge.agents import owner_memory

    channel = _Channel(MemoryReply(True, "recorded"))
    result = _call("Born in Izmir.", domain="biography", channel=channel)

    assert result.is_error is False
    assert "sent to Mark VI's memory just now" in result.content
    assert channel.sent == [{
        "skill": "record_observation",
        "content": "Born in Izmir.", "level": "explicit", "domain": "biography",
    }]
    assert not owner_memory.pending_path().exists()   # flushed, nothing left queued


def test_a_channel_that_fails_still_leaves_the_note_queued():
    from forge.agents import owner_memory

    channel = _Channel(MemoryReply(False, "the connection dropped"))
    result = _call("Will retry me.", channel=channel)

    assert result.is_error is False   # queuing itself never fails
    assert "did not go out this turn" in result.content
    assert "Will retry me" in owner_memory.pending_path().read_text(encoding="utf-8")


def test_a_broken_channel_does_not_fail_the_tool_call(monkeypatch):
    class _Boom:
        async def command(self, payload):
            raise RuntimeError("socket closed mid-flush")

    result = _call("Should not raise.", channel=_Boom())

    assert result.is_error is False
    assert "did not go out this turn" in result.content


def test_an_unknown_domain_falls_back_to_state():
    from forge.agents import owner_memory

    _call("x", domain="not-a-real-domain")

    assert '"domain": "state"' in owner_memory.pending_path().read_text(encoding="utf-8")
