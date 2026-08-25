"""A force-cancelled turn must not forget what it had already done (§3).

The one loss a forced cancel cannot avoid is the sentence being streamed at the
exact moment of the cancel — that text was never committed. Everything before it
— the operator's prompt, every tool round that already ran, every committed
assistant turn — was committed to the loop's transcript, and it must survive to
the next turn. A cancelled task has no Terminal to hand back, so the Warden keeps
its committed transcript reachable via `recover_transcript`, and the TUI's forced
cancel path uses it instead of re-seeding from the pre-turn session.
"""
import asyncio

from pydantic import BaseModel

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.tool import Tool, ToolContext, ToolResult


class _BlockArgs(BaseModel):
    pass


class Block(Tool):
    """Runs forever until its event is set — the shape of a command that hangs."""
    name = "block"
    description = "block until released"
    Args = _BlockArgs

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    async def call(self, args: _BlockArgs, ctx: ToolContext) -> ToolResult:
        await self.gate.wait()
        return ToolResult("released")


def _warden(gate: asyncio.Event) -> Warden:
    return Warden(
        system_prompt="",
        tools={"block": Block(gate)},
        model=ScriptedModel([
            lambda m: ("ask to block", [tool_call("block")]),
            lambda m: ("done", []),
        ]),
        ctx=ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                        permissions=PermissionEngine(), network_allowed=False),
    )


def test_recover_transcript_returns_the_committed_partial_turn():
    """Cancel mid-tool: the prompt and the committed assistant tool_use survive.

    The loop commits the assistant turn (with its `block` tool_use) BEFORE running
    the tool, so a cancel while the tool blocks must hand back a transcript that
    already contains the operator's prompt and that assistant turn — everything
    after the cancel is unrecoverable, but nothing before it is lost."""
    gate = asyncio.Event()
    warden = _warden(gate)

    async def drive() -> None:
        task = asyncio.create_task(warden.run("go"))
        # Let the loop commit the assistant turn and reach the blocking tool.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    committed = warden.recover_transcript()
    assert committed is not None

    # The seed: exactly the operator's prompt.
    assert committed[0]["role"] == "user"
    assert committed[0]["content"] == "go"

    # The committed assistant turn that asked for the tool, before the cancel.
    assert committed[1]["role"] == "assistant"
    blocks = committed[1]["content"]
    assert any(b.get("type") == "tool_use" and b.get("name") == "block"
               for b in blocks)


def test_recover_transcript_is_none_before_the_loop_starts():
    """Cancel before run_messages assigns state: nothing to recover, and the
    caller must fall back to the session's own transcript."""
    warden = _warden(asyncio.Event())
    assert warden.recover_transcript() is None
