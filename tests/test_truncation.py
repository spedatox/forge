"""A turn cut off at the output cap must not be reported as a finished one.

This is the one turn shape the transcript cannot distinguish on its own. A model
that runs out of output tokens produces text and no tool-use blocks — byte for
byte what a model that finished produces. Before the `TurnEnd` event existed the
loop read the second as the first and returned `COMPLETED` holding half a
sentence, which on the peer path is persisted as the answer.

The three properties below are what stop that: the fact reaches the loop, the
loop refuses to call it done, and the resume is bounded so a model that answers
'continue' by restarting the same paragraph cannot spin.
"""
import asyncio
from typing import Any, AsyncIterator

from pydantic import BaseModel

from forge.model.base import TextDelta, ToolUseRequest, TurnEnd, UsageReport
from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import MAX_TRUNCATION_RESUMES, Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import ContinueReason, StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult


class NudgeArgs(BaseModel):
    pass


class Nudge(Tool):
    name = "nudge"
    description = "do nothing, successfully"
    Args = NudgeArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: NudgeArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("ok")


def _ctx() -> ToolContext:
    return ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _warden(model, **kwargs) -> Warden:
    return Warden(system_prompt="sys", tools={"nudge": Nudge()}, model=model,
                  ctx=_ctx(), retry_attempts=0, **kwargs)


# ── The fact reaches the loop ────────────────────────────────────────────────

def test_truncated_turn_is_not_the_answer():
    """The corruption case: text, no tool calls, cut off at the cap.

    The job may well end COMPLETED — it does here, once the model is asked to
    continue and does. What must never happen is the half-sentence being
    returned as the final answer, which is exactly what happened before the
    guard existed."""
    fragment = "I will now write the file. It starts wi"
    model = ScriptedModel(steps=[lambda _m: (fragment, [])], ends=["max_tokens"])
    terminal = asyncio.run(_warden(model).run("do the thing"))

    assert terminal.final_text != fragment
    assert ContinueReason.RESUMED_TRUNCATED in [t.reason for t in terminal.transitions]


def test_untruncated_turn_still_completes():
    """The guard must not fire on a normal end — the common path is unchanged."""
    model = ScriptedModel(steps=[lambda _m: ("all done", [])], ends=["end_turn"])
    terminal = asyncio.run(_warden(model).run("do the thing"))

    assert terminal.reason is StopReason.COMPLETED
    assert terminal.final_text == "all done"


def test_silent_provider_keeps_old_behaviour():
    """A provider that reports nothing yields no TurnEnd. Absence is unknown, and
    unknown must fail OPEN here — treating every silent turn as truncated would
    resume forever. The cost is stated in `_Turn.truncated`."""
    model = ScriptedModel(steps=[lambda _m: ("all done", [])])   # no `ends`
    terminal = asyncio.run(_warden(model).run("do the thing"))

    assert terminal.reason is StopReason.COMPLETED


# ── The estimate, for providers that report usage but not a stop reason ──────

class UsageOnly:
    """Reports usage and no stop reason — the shape several OpenAI-compatible
    endpoints actually have. Truncation has to be inferred from output tokens
    reaching the cap."""
    model_id = "usage-only"

    def __init__(self, cap: int, output_tokens: int) -> None:
        self.cap = cap
        self.output_tokens = output_tokens
        self.calls = 0

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[Any]:
        self.calls += 1
        yield TextDelta("a partial answer that stops mid-")
        report = UsageReport(input_tokens=10, output_tokens=self.output_tokens)
        yield report
        yield TurnEnd(reason=None,
                      truncated_estimate=report.output_tokens >= self.cap)


def test_estimate_catches_truncation_without_a_stop_reason():
    model = UsageOnly(cap=100, output_tokens=100)
    terminal = asyncio.run(_warden(model).run("do the thing"))

    assert terminal.reason is not StopReason.COMPLETED
    assert model.calls > 1, "the loop should have asked the model to continue"


def test_estimate_does_not_fire_below_the_cap():
    model = UsageOnly(cap=100, output_tokens=42)
    terminal = asyncio.run(_warden(model).run("do the thing"))

    assert terminal.reason is StopReason.COMPLETED
    assert model.calls == 1


# ── The resume is bounded ────────────────────────────────────────────────────

class AlwaysTruncates:
    """The spiral this bound exists for: every turn hits the cap, so every
    resume produces another turn that hits the cap."""
    model_id = "always-truncates"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[Any]:
        self.calls += 1
        yield TextDelta("still going and going and")
        yield TurnEnd(reason="max_tokens")


def test_repeated_truncation_terminates_loudly():
    model = AlwaysTruncates()
    terminal = asyncio.run(_warden(model).run("write something enormous"))

    assert terminal.reason is StopReason.ERROR
    assert terminal.error and "token limit" in terminal.error
    # One initial turn plus the bounded resumes, and not one more.
    assert model.calls == MAX_TRUNCATION_RESUMES + 1


def test_resume_is_charged_against_the_iteration_budget():
    """A resumed turn is not work done. If it were free, a flaky cap could
    quietly extend every job past its ceiling."""
    model = AlwaysTruncates()
    warden = _warden(model)
    terminal = asyncio.run(warden.run("write something enormous"))

    assert terminal.reason is StopReason.ERROR
    resumed = [t for t in terminal.transitions
               if t.reason is ContinueReason.RESUMED_TRUNCATED]
    assert len(resumed) == MAX_TRUNCATION_RESUMES


def test_streak_resets_when_tools_run():
    """Consecutive, not cumulative. A long job that hits the cap once while
    writing each of several files is doing three recoveries, not spending three
    thirds of one budget."""
    model = ScriptedModel(
        steps=[
            lambda _m: ("writing part one and it runs ov", []),   # truncated
            lambda _m: ("...finished. checking.", [tool_call("nudge")]),
            lambda _m: ("writing part two and it runs ov", []),   # truncated
            lambda _m: ("...finished. checking.", [tool_call("nudge")]),
            lambda _m: ("all done", []),
        ],
        ends=["max_tokens", "tool_use", "max_tokens", "tool_use", "end_turn"],
    )
    terminal = asyncio.run(_warden(model).run("write two files"))

    assert terminal.reason is StopReason.COMPLETED
    resumed = [t for t in terminal.transitions
               if t.reason is ContinueReason.RESUMED_TRUNCATED]
    assert len(resumed) == 2, "both truncations recovered; neither exhausted the bound"


# ── Truncation mid-tool_use is somebody else's job ───────────────────────────

class TruncatedMidToolUse:
    """Cut off while emitting a tool call, so its arguments are incomplete.

    The loop deliberately does NOT intervene here: the partial input fails the
    tool's own schema validation and comes back as an error naming the correct
    signature, which is a better message than a generic resume. This test exists
    to confirm the graceful path holds rather than to assert new behaviour."""
    model_id = "truncated-mid-tool"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[Any]:
        self.calls += 1
        if self.calls == 1:
            yield TextDelta("calling the tool")
            # Name truncated away entirely — the worst-case partial block.
            yield ToolUseRequest(id="toolu_1", name="", input={})
            yield TurnEnd(reason="max_tokens")
            return
        yield TextDelta("recovered")
        yield TurnEnd(reason="end_turn")


def test_truncation_mid_tool_use_falls_through_to_validation():
    model = TruncatedMidToolUse()
    terminal = asyncio.run(_warden(model).run("do the thing"))

    assert terminal.reason is StopReason.COMPLETED
    # The malformed call was answered as an error the model could act on, not
    # swallowed by the truncation path.
    blocks = [b for m in terminal.messages if isinstance(m.get("content"), list)
              for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert any(b.get("is_error") for b in blocks)
    assert not any(t.reason is ContinueReason.RESUMED_TRUNCATED
                   for t in terminal.transitions)
