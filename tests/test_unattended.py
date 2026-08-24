"""A dispatched job has no live operator behind it; a chat turn does.

Mark VI's wire frame for a dispatch (`{type, task_id, from, task, cwd,
permission_mode}` — see `packages/igor/app/core/dispatch.py:_run_external` in
speda-mark6) carries no "blocking vs background" distinction, and never will:
Mark VI keeps that split on its own side (`dispatch()` awaits inline, `spawn()`
wakes the caller later). What Forge CAN observe is the frame type itself —
task_dispatch vs chat_request — and that line exactly matches the one fact that
is always true of a dispatch and never of a chat: nothing on this end of the
socket is a human who can answer a follow-up in the next few seconds.

Without this, an agent given "prototype an app while I'm away" behaved exactly
as it would in an interactive chat: no signal that its final answer is the only
thing anyone will read, no signal that `ask_operator` will most likely time out
unanswered. `JobRequest.unattended` carries the fact; `run_job` turns it into a
labelled system-prompt fragment so the model knows before it starts.
"""
from __future__ import annotations

import asyncio

from forge.gate.protocol import JobRequest, job_from_chat_request, job_from_task_dispatch
from forge.model.base import TextDelta


def test_task_dispatch_is_marked_unattended():
    frame = {"task_id": "t1", "from": "speda", "task": "prototype an app",
             "cwd": "/repo", "permission_mode": None}
    jr = job_from_task_dispatch(frame, "optimus")
    assert jr.unattended is True


def test_chat_request_is_not_marked_unattended():
    frame = {"chat_id": "c1", "cwd": "/repo", "history": [
        {"role": "user", "content": "hi"}]}
    jr = job_from_chat_request(frame, "optimus")
    assert jr.unattended is False


def test_unattended_defaults_false_for_a_bare_job_request():
    """A caller that builds a JobRequest directly (tests, the demo) gets the
    chat-like default — the flag is opt-in, not opt-out."""
    assert JobRequest(agent="optimus", task="do it").unattended is False


class _SystemPromptSpy:
    """Captures the system prompt handed to a turn; produces no tool calls, so
    the run completes after one turn."""

    model_id = "spy"

    def __init__(self):
        self.seen_system: list[str] = []

    async def stream(self, *, system, messages, tools, signal):
        self.seen_system.append(system)
        yield TextDelta("done")


def _run_job_kwargs():
    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeSettings
    return dict(settings=ForgeSettings.from_env(), registry=AgentRegistry.load())


async def _sink(_ev):
    return None


def test_run_job_adds_the_unattended_fragment_for_a_dispatch():
    from forge.gate.runner import run_job

    spy = _SystemPromptSpy()
    request = JobRequest(agent="optimus", task="prototype an app", unattended=True)
    asyncio.run(run_job(request, emit=_sink, model=spy, **_run_job_kwargs()))

    assert len(spy.seen_system) == 1
    assert "UNATTENDED" in spy.seen_system[0]
    assert "ask_operator" in spy.seen_system[0]


def test_run_job_omits_the_unattended_fragment_for_a_chat():
    from forge.gate.runner import run_job

    spy = _SystemPromptSpy()
    request = JobRequest(agent="optimus", task="hi", unattended=False)
    asyncio.run(run_job(request, emit=_sink, model=spy, **_run_job_kwargs()))

    assert len(spy.seen_system) == 1
    assert "UNATTENDED" not in spy.seen_system[0]
