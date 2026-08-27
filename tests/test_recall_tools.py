"""Recall over past sessions, and the shared agent channel — both reached over
the same peer socket the `memory` tool uses.

The peer is stateless between turns: Mark VI sends the history each turn and
nothing survives on the Forge side. So a PAST session — a conversation that is
not the current one — has exactly one route home, the vectors and FTS index in
Mark VI's database, and `recall_conversations` is that route. `read_agent_channel`
is the other half of "know the wider world": the shared inter-agent log, which
is how the two Forge peers see each other's work.

Both are channels, not stores (the rule the whole memory design is built on):
nothing is embedded or cached here, the backend runs the SAME skills its
in-process agents run, and this end only builds a frame and returns what came
back. The tests below hold that shape, and the failure direction that matters
for a read the model will trust — an empty recall is an answer, a dead channel
is an error.
"""
from __future__ import annotations

import asyncio

from forge.tools.recall import (MAX_CHARS, AgentChannelArgs, ReadAgentChannel,
                                RecallArgs, RecallConversations)
from forge.warden.memory import MemoryReply
from forge.warden.tool import ToolContext


class _Channel:
    """Stands in for Mark VI at the other end of the socket."""

    def __init__(self, reply: MemoryReply | None = None) -> None:
        self.reply = reply or MemoryReply(True, "ok")
        self.sent: list[dict] = []

    async def command(self, payload: dict) -> MemoryReply:
        self.sent.append(payload)
        return self.reply


def _ctx(memory=None) -> ToolContext:
    return ToolContext(agent_id="optimus", cell=None, graph=None, files=None,
                       permissions=None, network_allowed=False, memory=memory)


def _recall(args: dict, channel=None):
    return asyncio.run(RecallConversations().call(RecallArgs(**args), _ctx(channel)))


def _channel_read(args: dict, channel=None):
    return asyncio.run(ReadAgentChannel().call(AgentChannelArgs(**args), _ctx(channel)))


# ── recall_conversations: the frame it builds ────────────────────────────────

def test_a_recall_names_its_skill_and_carries_its_query():
    channel = _Channel(MemoryReply(True, "Found 2 relevant exchange(s)..."))
    result = _recall({"query": "the database migration", "after": "2026-06-01",
                      "limit": 5}, channel)

    assert result.is_error is False
    assert "Found 2 relevant" in result.content
    assert channel.sent == [{"skill": "recall_conversations",
                             "query": "the database migration",
                             "after": "2026-06-01", "limit": 5}]


def test_recall_never_sends_unset_arguments_as_nulls():
    """An absent `before` sent as null invites the far side to read "no upper
    bound" as "on or before nothing"."""
    channel = _Channel()
    _recall({"query": "x"}, channel)

    assert set(channel.sent[0]) == {"skill", "query"}


def test_recall_can_be_scoped_to_one_agent():
    """agent_id is how a peer asks 'what did I say' vs 'what did the roster say' —
    it must reach the far side, not be dropped as an unknown field."""
    channel = _Channel()
    _recall({"query": "scope", "agent_id": "centurion"}, channel)

    assert channel.sent[0]["agent_id"] == "centurion"


# ── read_agent_channel: the frame it builds ──────────────────────────────────

def test_the_channel_read_names_its_skill():
    channel = _Channel(MemoryReply(True, "AGENT NETWORK CHANNEL..."))
    result = _channel_read({"agent": "centurion", "limit": 10}, channel)

    assert result.is_error is False
    assert channel.sent == [{"skill": "read_agent_channel",
                             "agent": "centurion", "limit": 10}]


def test_the_channel_read_with_no_arguments_is_just_the_skill():
    channel = _Channel()
    _channel_read({}, channel)

    assert channel.sent[0] == {"skill": "read_agent_channel"}


# ── The failure direction ────────────────────────────────────────────────────

def test_no_channel_is_an_error_that_says_what_mode_this_is():
    """The standalone TUI. Both tools are normally withheld here — this is the
    belt, for an embedder that wired one without a backend."""
    for result in (_recall({"query": "x"}), _channel_read({})):
        assert result.is_error is True
        assert "no connection to Mark VI" in result.content
        # It must not invite a retry or let the agent claim it recalled anything.
        assert "do not claim" in result.content


def test_an_empty_recall_is_an_answer_not_an_error():
    """'No relevant past exchanges' is a real result written for a model. Marking
    it is_error would teach the agent that recall is flaky and send it retrying
    the one query that already ran."""
    channel = _Channel(MemoryReply(True, "No relevant past exchanges found for 'x'."))
    result = _recall({"query": "x"}, channel)

    assert result.is_error is False
    assert "No relevant past exchanges" in result.content


def test_a_dead_backend_is_reported_as_an_error():
    """ok=False is the one case the peer must believe nothing about: the read did
    not run, so the agent must not report what it 'found'."""
    channel = _Channel(MemoryReply(False, "the connection to Mark VI closed"))
    result = _recall({"query": "x"}, channel)

    assert result.is_error is True
    assert "closed" in result.content


def test_a_long_result_is_truncated_and_says_so():
    channel = _Channel(MemoryReply(True, "x" * (MAX_CHARS + 5_000)))
    result = _recall({"query": "x"}, channel)

    assert len(result.content) < MAX_CHARS + 500
    assert "truncated" in result.content


# ── Safety flags ─────────────────────────────────────────────────────────────

def test_both_reads_are_read_only_and_may_share_a_batch():
    """Two reads never interfere; forcing them to serialize would make recalling
    while reading the channel needlessly sequential."""
    r, rargs = RecallConversations(), RecallArgs(query="x")
    c, cargs = ReadAgentChannel(), AgentChannelArgs()

    assert r.is_read_only(rargs) and r.is_concurrency_safe(rargs)
    assert c.is_read_only(cargs) and c.is_concurrency_safe(cargs)


# ── Offered only where it can work ───────────────────────────────────────────

def test_the_tools_are_withheld_when_there_is_no_backend():
    from forge.warden.toolsource import without_recall_tools

    stripped = without_recall_tools(
        {"recall_conversations": object(), "read_agent_channel": object(),
         "read_file": object()})
    assert "recall_conversations" not in stripped
    assert "read_agent_channel" not in stripped
    assert "read_file" in stripped


def test_a_job_with_no_channel_never_sees_them():
    """Same per-job filter as the memory tool: one Forge serves connected peer
    runs and disconnected TUI runs, so this cannot be settled once at startup."""
    import inspect

    from forge.gate import runner

    src = inspect.getsource(runner.run_job)
    assert "if ctx.memory is None:" in src
    assert "without_recall_tools(built)" in src


def test_the_standalone_tui_is_not_offered_them():
    import inspect

    from forge.tui import repl

    assert "without_recall_tools(" in inspect.getsource(repl._run_turn)


def test_the_peer_agents_are_actually_given_them():
    """Everything above is unreachable in the running system without this. Both
    peers get the group, because both are meant to recall and to know the other."""
    from forge.agents.registry import AgentRegistry

    registry = AgentRegistry.load()
    for agent_id in ("optimus", "centurion"):
        names = registry.get(agent_id).tool_names
        assert "recall_conversations" in names, agent_id
        assert "read_agent_channel" in names, agent_id


def test_a_profile_can_ask_for_recall_without_the_power_to_rewrite_memory():
    """Its own group, separate from 'memory': reading what was said is not the
    same capability as rewriting what every agent believes."""
    from forge.agents.registry import _TOOL_GROUPS

    assert _TOOL_GROUPS["recall"] == ("recall_conversations", "read_agent_channel")
    assert "recall_conversations" not in _TOOL_GROUPS["memory"]
