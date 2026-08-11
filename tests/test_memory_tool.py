"""The owner's memory, which lives in Mark VI and is reached over the socket.

Reported as "the Forge agent cannot read or write their memories" after Mark VI
redesigned its memory system. Nothing in the Forge broke — a contract did. The
injected block stopped being "here is everything" and became "here is a
directory listing and four preloaded files, use the `memory` tool for the
rest". Every in-process agent has that tool. The peer had never been given one,
so the block arriving over the socket was instructing an agent to call
something that did not exist: no project file, no person, no ledger, and no way
to write down anything it learned.

The tool is a channel and not a store. Mark VI keeps the schema, the revision
trail, the per-document ownership and the custodian; a peer holding its own
copy is the memory that quietly forks, which is the failure the whole
architecture is arranged to avoid.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tools.memory import MAX_CHARS, Memory, MemoryArgs
from forge.warden.memory import MemoryReply, RemoteMemory
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


def _call(args: dict, channel=None):
    tool = Memory()
    return asyncio.run(tool.call(MemoryArgs(**args), _ctx(channel)))


# ── Reading and writing ──────────────────────────────────────────────────────

def test_a_read_reaches_the_backend_and_comes_back():
    channel = _Channel(MemoryReply(True, "# Ahmet's projects\n..."))
    result = _call({"command": "view", "path": "/memories/projects/forge.md"}, channel)

    assert result.is_error is False
    assert "Ahmet's projects" in result.content
    assert channel.sent == [{"command": "view", "path": "/memories/projects/forge.md"}]


def test_a_write_carries_every_argument_it_was_given():
    """Dropping one silently is the worst available failure: the call succeeds,
    the model believes the fact is filed, and it is not."""
    channel = _Channel()
    _call({"command": "str_replace", "path": "/memories/current.md",
           "old_str": "was", "new_str": "is"}, channel)

    assert channel.sent[0] == {"command": "str_replace", "path": "/memories/current.md",
                               "old_str": "was", "new_str": "is"}


def test_unset_arguments_are_not_sent_as_nulls():
    """A `view` carrying `file_text: null` invites the far side to treat an
    absent argument as an empty one."""
    channel = _Channel()
    _call({"command": "view", "path": "/memories/log.md"}, channel)

    assert set(channel.sent[0]) == {"command", "path"}


# ── The failure direction ────────────────────────────────────────────────────

def test_no_channel_is_an_error_that_says_what_mode_this_is():
    """The standalone TUI. The tool is normally withheld here — this is the
    belt, for an embedder that wired one without a backend."""
    result = _call({"command": "view", "path": "/memories/log.md"})

    assert result.is_error is True
    assert "no connection to Mark VI" in result.content
    # It must not invite a retry, and it must not let the agent claim it looked.
    assert "do not claim" in result.content


def test_a_failed_write_is_reported_as_a_failure():
    """The one thing this must never do is succeed quietly. An agent that
    believes it filed something and did not has lost the fact AND the knowledge
    that it lost it."""
    channel = _Channel(MemoryReply(False, "the connection to Mark VI closed"))
    result = _call({"command": "create", "path": "/memories/projects/x.md",
                    "file_text": "..."}, channel)

    assert result.is_error is True
    assert "closed" in result.content


def test_an_empty_answer_is_not_mistaken_for_a_failure():
    """`delete` succeeds and says almost nothing. Inferring the verdict from the
    body would report the one operation with no output as broken."""
    channel = _Channel(MemoryReply(True, ""))
    result = _call({"command": "delete", "path": "/memories/projects/old.md"}, channel)

    assert result.is_error is False


def test_a_long_file_is_truncated_and_says_so():
    channel = _Channel(MemoryReply(True, "x" * (MAX_CHARS + 5_000)))
    result = _call({"command": "view", "path": "/memories/log.md"}, channel)

    assert len(result.content) < MAX_CHARS + 500
    assert "truncated" in result.content
    assert "do not conclude anything from what is missing" in result.content


# ── Safety flags, answered per command ───────────────────────────────────────

def test_a_view_may_share_a_batch_and_a_write_may_not():
    tool = Memory()
    view = MemoryArgs(command="view", path="/memories/log.md")
    write = MemoryArgs(command="str_replace", path="/memories/log.md",
                       old_str="a", new_str="b")

    assert tool.is_read_only(view) and tool.is_concurrency_safe(view)
    assert not tool.is_read_only(write) and not tool.is_concurrency_safe(write)


def test_the_schema_matches_mark_vi_s_argument_names():
    """A model that learned memory on one engine must not have to relearn it
    because a different one ran the turn — and the failure would be silent:
    `file_text` renamed here becomes content the far side never receives."""
    fields = set(MemoryArgs.model_fields)

    assert fields == {"command", "path", "file_text", "old_str", "new_str",
                      "insert_line", "insert_text", "view_range"}


def test_every_command_mark_vi_implements_is_describable():
    described = Memory.description + MemoryArgs.model_fields["command"].description
    for command in ("view", "create", "str_replace", "insert", "delete"):
        assert command in described, command


# ── The channel: parking, correlating, and giving up ─────────────────────────

def test_a_reply_resolves_the_call_that_is_waiting():
    async def scenario():
        sent: list[dict] = []

        async def send(frame):
            sent.append(frame)

        channel = RemoteMemory(send)
        call = asyncio.create_task(
            channel.command({"command": "view", "path": "/memories/log.md"}))
        await asyncio.sleep(0)
        request_id = sent[0]["request_id"]
        channel.resolve(request_id, MemoryReply(True, "the log"))
        return await call

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert reply.ok and reply.text == "the log"


def test_a_lost_socket_fails_the_call_instead_of_hanging():
    async def scenario():
        async def send(_frame):
            return None

        channel = RemoteMemory(send)
        call = asyncio.create_task(channel.command({"command": "create", "path": "/x"}))
        await asyncio.sleep(0)
        channel.abandon_all("the connection to Mark VI closed before this was saved")
        return await call

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert reply.ok is False
    assert "before this was saved" in reply.text


def test_an_unsendable_request_does_not_raise_into_the_turn():
    async def scenario():
        async def send(_frame):
            raise ConnectionError("peer socket not connected")

        return await RemoteMemory(send).command({"command": "view", "path": "/x"})

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert reply.ok is False
    assert "could not be reached" in reply.text


def test_a_call_that_is_never_answered_gives_up():
    async def scenario():
        async def send(_frame):
            return None

        return await RemoteMemory(send, timeout_s=0.2).command({"command": "view",
                                                                "path": "/x"})

    reply = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert reply.ok is False
    assert "did not answer" in reply.text


def test_a_late_reply_to_an_abandoned_call_is_not_an_error():
    """A slow backend is not a bug, and treating it as one would log noise on
    every timeout."""
    async def send(_frame):
        return None

    assert RemoteMemory(send).resolve("never-parked", MemoryReply(True, "x")) is False


def test_a_scoped_channel_labels_its_frames_and_shares_the_parking_lot():
    """The peer runs several conversations over one socket. Mark VI needs to
    know whose turn is writing, and a reply must land wherever it was parked
    regardless of which view sent it."""
    async def scenario():
        sent: list[dict] = []

        async def send(frame):
            sent.append(frame)

        parent = RemoteMemory(send)
        child = parent.scoped("chat-7")
        call = asyncio.create_task(child.command({"command": "view", "path": "/x"}))
        await asyncio.sleep(0)
        assert sent[0]["chat_id"] == "chat-7"
        # Resolved through the PARENT — one socket, one frame handler.
        assert parent.resolve(sent[0]["request_id"], MemoryReply(True, "done")) is True
        return await call

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=5)).ok


# ── Offered only where it can work ───────────────────────────────────────────

def test_the_tool_is_withheld_when_there_is_no_backend():
    """Same rule as the graph and vault tools. A tool that can only fail costs
    a call to discover and teaches the model that memory is flaky — and here it
    is worse, because the prompt has already told it to use one."""
    from forge.warden.toolsource import without_memory_tools

    assert "memory" not in without_memory_tools({"memory": object(), "read_file": object()})
    assert "read_file" in without_memory_tools({"memory": object(), "read_file": object()})


def test_a_job_with_no_channel_never_sees_it():
    """The filter is per JOB, not per deployment: the same Forge serves peer
    runs that have a backend and TUI runs that do not."""
    import inspect

    from forge.gate import runner

    src = inspect.getsource(runner.run_job)
    assert "if ctx.memory is None:" in src
    assert "without_memory_tools(built)" in src


def test_the_peer_hands_its_channel_to_every_job():
    import inspect

    from forge.gate import peer

    for handler in (peer.ForgePeer._handle_chat, peer.ForgePeer._handle_task):
        assert "memory=self._memory" in inspect.getsource(handler), handler.__name__


def test_a_profile_can_ask_for_memory_without_asking_for_everything():
    """Its own group, like the vault: the owner's memory is not a capability of
    working on a repository, and it is shared with every agent that serves him."""
    from forge.agents.registry import _TOOL_GROUPS

    assert _TOOL_GROUPS["memory"] == ("memory",)
    assert "memory" not in _TOOL_GROUPS["coding"]


def test_the_peer_agent_is_actually_given_it():
    """Everything above is unreachable in the running system without this."""
    from forge.agents.registry import AgentRegistry

    assert "memory" in AgentRegistry.load().get("optimus").tool_names


@pytest.mark.parametrize("frame_type", ["memory_response"])
def test_the_peer_routes_the_answer_home(frame_type):
    import inspect

    from forge.gate import peer

    src = inspect.getsource(peer.ForgePeer._dispatch)
    assert frame_type in src
    # ok travels explicitly rather than being guessed from the body
    assert 'frame.get("ok"' in src


def test_the_standalone_tui_is_not_offered_it():
    """The offline path never has Mark VI on the other end, and unlike the
    graph nothing there can bring one into existence mid-session. Offering it
    would put a permanently-failing tool in front of an agent whose prompt has
    just told it that memory is unreachable."""
    import inspect

    from forge.tui import repl

    src = inspect.getsource(repl._run_turn)
    assert "without_memory_tools(session.tools)" in src


def test_the_offline_prompt_still_says_memory_is_unreachable():
    """The tool being absent is the mechanism; this is the agent being told,
    which is what stops it reporting facts it has not checked."""
    from forge.agents import owner_memory

    assert "cannot read or write the owner's memory" in inspect_source(owner_memory)


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)
