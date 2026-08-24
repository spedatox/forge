"""A model's SDK client must be closed exactly when run_job owns it.

Found while debugging a recurring, harmless-looking `httpcore2` traceback that
followed every real (non-scripted) run: `OpenAICompatModel` and `AnthropicModel`
each hold a real SDK client — an httpx connection pool underneath — and nothing
ever closed it. `run_job`'s `finally` already closes the Cell, the graph sidecar
and every tool provider; the model was the one resource left for GC to find,
which for an async client means a teardown outside the event loop's control.

In the peer's long-lived process (`forge connect`), `_build_model` runs once per
chat turn, so this was not just log noise but one leaked connection pool per
turn. `_FallbackModel` made it worse: it builds a fresh sub-model on every
`stream()` call (once per turn, not once per job), including refs that lost the
race, so a configured fallback chain leaked one client per ref tried, per turn.

Ownership is the deciding rule, matching every other resource in `run_job`'s
`finally`: a model `run_job` BUILT (the caller passed `model=None`), it also
closes; a model the caller INJECTED (tests, the demo's ScriptedModel) is the
caller's to keep alive or discard across other calls, so run_job leaves it
alone. `close` is checked, not assumed, the same way `UsageReport` is optional
by contract elsewhere in this package — a model with no client to release
(ScriptedModel) simply has no `close` method.
"""
from __future__ import annotations

import asyncio

import forge.gate.runner as runner_mod
from forge.gate.protocol import JobRequest
from forge.model.scripted import ScriptedModel


class _CloseSpy:
    """A ScriptedModel-shaped double that also tracks whether it was closed."""

    model_id = "spy"

    def __init__(self):
        self.closed = 0

    async def stream(self, *, system, messages, tools, signal):
        from forge.model.base import TextDelta
        yield TextDelta("done")

    async def close(self):
        self.closed += 1


def _run_job_kwargs():
    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeSettings
    return dict(settings=ForgeSettings.from_env(), registry=AgentRegistry.load())


async def _sink(_ev):
    return None


def test_run_job_closes_a_model_it_built(monkeypatch):
    """model=None -> run_job calls _build_model -> run_job must close it."""
    spy = _CloseSpy()
    monkeypatch.setattr(runner_mod, "_build_model", lambda ref, settings, max_tokens: spy)

    request = JobRequest(agent="optimus", task="hi")
    asyncio.run(runner_mod.run_job(request, emit=_sink, model=None, **_run_job_kwargs()))

    assert spy.closed == 1, "a model run_job built itself must be closed exactly once"


def test_run_job_leaves_a_caller_supplied_model_open():
    """model=<instance> -> the caller owns it; run_job must not touch it."""
    spy = _CloseSpy()
    request = JobRequest(agent="optimus", task="hi")
    asyncio.run(runner_mod.run_job(request, emit=_sink, model=spy, **_run_job_kwargs()))

    assert spy.closed == 0, "an injected model belongs to the caller, not run_job"


def test_run_job_tolerates_a_model_with_no_close(monkeypatch):
    """ScriptedModel (and any minimal Model) has no `close` — run_job must not
    require one; `close` is a capability, checked via getattr, not assumed."""
    monkeypatch.setattr(runner_mod, "_build_model",
                        lambda ref, settings, max_tokens: ScriptedModel([lambda _m: ("hi", [])]))
    request = JobRequest(agent="optimus", task="hi")
    term = asyncio.run(runner_mod.run_job(request, emit=_sink, model=None, **_run_job_kwargs()))
    assert term.reason.value == "completed"   # no AttributeError from a missing close


def test_fallback_model_close_sweeps_every_built_sub(monkeypatch):
    """_FallbackModel builds a fresh sub PER stream() call, including refs that
    lost the race to open. close() must close all of them, not just the winner —
    the failed-to-open ones still hold a real (if unused) SDK client."""
    from forge.model import factory

    built: list[_CloseSpy] = []

    def _fake_build_single(ref, settings, max_tokens):
        if ref == "bad":
            raise RuntimeError("no key configured")
        spy = _CloseSpy()
        built.append(spy)
        return spy

    monkeypatch.setattr(factory, "_build_single", _fake_build_single)

    fm = factory._FallbackModel(["bad", "good"], settings=None, max_tokens=100)

    async def drive():
        events = [ev async for ev in fm.stream(system="", messages=[], tools=[],
                                                signal=asyncio.Event())]
        return events

    events = asyncio.run(drive())
    assert len(events) == 1                      # the "good" ref's one TextDelta
    assert len(built) == 1                        # "bad" never reached _build_single successfully
    asyncio.run(fm.close())
    assert built[0].closed == 1


def test_fallback_model_close_survives_one_client_failing_to_close(monkeypatch):
    """One sub's close() raising must not stop the others from being closed —
    teardown is best-effort, matching cell/graph/provider teardown elsewhere."""
    from forge.model import factory

    class _Boom(_CloseSpy):
        async def close(self):
            raise RuntimeError("already gone")

    order = ["boom", "fine"]
    built = {"boom": _Boom(), "fine": _CloseSpy()}
    monkeypatch.setattr(factory, "_build_single", lambda ref, settings, max_tokens: built[ref])

    fm = factory._FallbackModel(order, settings=None, max_tokens=100)

    async def drive():
        async for _ in fm.stream(system="", messages=[], tools=[], signal=asyncio.Event()):
            pass
        # Force both refs to have been built, not just the first that opened.
        fm._built = [built["boom"], built["fine"]]
        await fm.close()

    asyncio.run(drive())
    assert built["fine"].closed == 1, "a sibling's close() failing must not block this one"
