"""What Mark VI knows about the owner, and what survives losing Mark VI.

Two properties matter here and both fail silently when broken.

The first is that the memory arrives at all. Before this existed the peer ran
the owner's turns knowing the conversation and nothing about the owner — and
nothing anywhere errored, because a missing background fact does not raise. It
just produces an agent that has never met you.

The second is that the offline snapshot is never mistaken for live memory. An
agent handed stale facts without being told they are stale states them as
current, which is a worse failure than having no memory at all: silence is
obviously missing information, and a confident wrong answer is not.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from forge.agents import owner_memory
from forge.gate.protocol import job_from_chat_request


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the real ~/.forge during tests."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    return tmp_path


BLOCK = "## Owner\nAhmet Erol. Prefers concise answers.\n"


# ── It arrives over the wire ─────────────────────────────────────────────────


def test_the_memory_block_is_carried_off_the_chat_request():
    job = job_from_chat_request(
        {"chat_id": "c1", "history": [{"role": "user", "content": "hi"}],
         "memory_block": BLOCK},
        "optimus")
    assert job.memory_block == BLOCK


def test_a_backend_that_sends_no_memory_still_works():
    """An older Mark VI, or a plain dispatch. Absence is not an error."""
    job = job_from_chat_request(
        {"chat_id": "c1", "history": [{"role": "user", "content": "hi"}]}, "optimus")
    assert job.memory_block == ""


def test_a_live_block_becomes_a_labelled_fragment():
    frag = owner_memory.live_fragment(BLOCK)
    assert frag is not None
    assert "live" in frag.source.lower()
    assert "Ahmet Erol" in frag.text


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_nothing_is_injected_when_there_is_nothing_to_inject(empty):
    assert owner_memory.live_fragment(empty) is None


def test_an_enormous_block_is_bounded():
    frag = owner_memory.live_fragment("x" * (owner_memory.MAX_CHARS * 3))
    assert len(frag.text) <= owner_memory.MAX_CHARS


# ── The offline snapshot ─────────────────────────────────────────────────────


def test_no_snapshot_means_no_fragment_not_a_crash():
    assert owner_memory.offline_fragment() is None


def test_what_arrives_online_is_readable_offline():
    """The countermeasure in one line: the peer path caches, the standalone
    path reads."""
    owner_memory.remember(BLOCK)
    frag = owner_memory.offline_fragment()

    assert frag is not None
    assert "Ahmet Erol" in frag.text


def test_the_offline_fragment_says_it_is_a_snapshot():
    """The property that stops stale facts being stated as current."""
    owner_memory.remember(BLOCK)
    text = owner_memory.offline_fragment().text.lower()

    assert "snapshot" in text
    assert "offline" in text
    assert "out of date" in text


def test_the_offline_fragment_says_remember_about_owner_still_works():
    """The snapshot itself is still read-only, but the agent has a real
    write-back path now (`remember_about_owner`, queued locally) — the header
    must say so rather than claim offline writes are impossible."""
    owner_memory.remember(BLOCK)
    text = owner_memory.offline_fragment().text
    assert "remember_about_owner" in text
    assert "next time this peer connects" in text


def test_the_snapshot_is_dated_in_words():
    owner_memory.remember(BLOCK)
    assert "captured today" in owner_memory.offline_fragment().text


def test_an_old_snapshot_reports_its_age(_isolated_home):
    """A bare timestamp asks the model to do date arithmetic mid-turn, which it
    does badly and silently."""
    stamp = (_dt.datetime.now().astimezone() - _dt.timedelta(days=9)).isoformat(
        timespec="seconds")
    owner_memory.snapshot_path().parent.mkdir(parents=True, exist_ok=True)
    owner_memory.snapshot_path().write_text(
        f"<!-- captured {stamp} -->\n{BLOCK}\n", encoding="utf-8")

    assert "captured 9 days ago" in owner_memory.offline_fragment().text


def test_a_snapshot_without_a_stamp_is_still_usable():
    owner_memory.snapshot_path().parent.mkdir(parents=True, exist_ok=True)
    owner_memory.snapshot_path().write_text(BLOCK, encoding="utf-8")

    frag = owner_memory.offline_fragment()
    assert "Ahmet Erol" in frag.text and "unknown age" in frag.text


def test_a_later_block_replaces_an_earlier_one():
    """One snapshot, always the latest. Appending would grow without bound and
    contradict itself."""
    owner_memory.remember("## Owner\nold fact\n")
    owner_memory.remember("## Owner\nnew fact\n")

    text = owner_memory.offline_fragment().text
    assert "new fact" in text and "old fact" not in text


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_block_does_not_clobber_a_good_snapshot(empty):
    """A dispatch carries no memory block. It must not wipe what a chat turn
    cached, or one background job would erase the offline countermeasure."""
    owner_memory.remember(BLOCK)
    owner_memory.remember(empty)

    assert "Ahmet Erol" in owner_memory.offline_fragment().text


def test_an_unwritable_home_does_not_fail_the_turn(monkeypatch):
    """Caching is a convenience. Failing to write it must never fail the job
    that happened to carry the block."""
    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(owner_memory.Path, "mkdir", _boom)
    owner_memory.remember(BLOCK)          # must not raise


def test_the_snapshot_is_per_user_not_per_repository():
    """It describes the owner, who is the same person in every repo."""
    assert owner_memory.snapshot_path().name == owner_memory.SNAPSHOT_NAME


# ── The write-back path: queue locally, flush on reconnect ──────────────────


class _FakeChannel:
    """A MemoryChannel stand-in — records every payload it was asked to send
    and answers however the test tells it to, without a socket."""

    def __init__(self, replies=None):
        self.sent: list[dict] = []
        self._replies = list(replies) if replies is not None else None

    async def command(self, payload):
        from forge.warden.memory import MemoryReply

        self.sent.append(payload)
        if self._replies is not None:
            return self._replies.pop(0)
        return MemoryReply(True, "recorded")


def test_queue_observation_needs_no_channel_at_all():
    """The entire point: this works in a run with no connection to Mark VI."""
    owner_memory.queue_observation("He prefers concise answers.")

    raw = owner_memory.pending_path().read_text(encoding="utf-8")
    assert "concise answers" in raw
    assert '"level": "explicit"' in raw
    assert '"domain": "state"' in raw


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_note_is_not_queued(empty):
    owner_memory.queue_observation(empty)
    assert not owner_memory.pending_path().exists()


def test_a_domain_can_be_named():
    owner_memory.queue_observation("Born in Izmir.", domain="biography")
    assert '"domain": "biography"' in owner_memory.pending_path().read_text(encoding="utf-8")


def test_no_pending_file_means_flush_is_a_quiet_noop():
    sent = asyncio.run(owner_memory.flush_pending(_FakeChannel()))
    assert sent == 0


def test_flush_sends_every_queued_line_and_empties_the_queue():
    owner_memory.queue_observation("First fact.")
    owner_memory.queue_observation("Second fact.", domain="biography")
    channel = _FakeChannel()

    sent = asyncio.run(owner_memory.flush_pending(channel))

    assert sent == 2
    assert [p["content"] for p in channel.sent] == ["First fact.", "Second fact."]
    assert all(p["skill"] == "record_observation" for p in channel.sent)
    assert not owner_memory.pending_path().exists()


def test_a_line_mark_vi_could_not_run_stays_queued():
    """ok=False means the command never ran (peer_memory.py's contract) — that
    is the one case worth retrying, unlike a refusal Mark VI actually ran."""
    from forge.warden.memory import MemoryReply

    owner_memory.queue_observation("Will retry me.")
    channel = _FakeChannel(replies=[MemoryReply(False, "the connection dropped")])

    sent = asyncio.run(owner_memory.flush_pending(channel))

    assert sent == 0
    assert "Will retry me" in owner_memory.pending_path().read_text(encoding="utf-8")


def test_a_refusal_mark_vi_actually_ran_is_not_retried():
    """ok=True means it ran, even if what ran was a refusal — that counts as
    resolved (peer_memory.py's own contract), and the line is dropped exactly
    like a successful record. Retrying a refusal would only produce the same
    refusal again."""
    from forge.warden.memory import MemoryReply

    owner_memory.queue_observation("Refused for some reason.")
    channel = _FakeChannel(replies=[MemoryReply(True, "refused: not your document")])

    sent = asyncio.run(owner_memory.flush_pending(channel))

    assert sent == 1
    assert not owner_memory.pending_path().exists()


def test_a_corrupted_line_is_dropped_not_retried_forever():
    owner_memory.pending_path().parent.mkdir(parents=True, exist_ok=True)
    owner_memory.pending_path().write_text("not json\n", encoding="utf-8")

    sent = asyncio.run(owner_memory.flush_pending(_FakeChannel()))

    assert sent == 0
    assert not owner_memory.pending_path().exists()
