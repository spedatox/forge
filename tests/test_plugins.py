"""The plugin system: the contract, the waterfall, and taking it back out.

Ported from DSH's architecture, whose load-bearing idea is not "plugins" — Forge
already had provider and hook seams — but `next()`. A hook can inspect, veto or
rewrite; only an around-listener can WRAP, and wrapping is what a timeout, a
retry, a tracer or a cache actually is. Forge's own tool deadline is the proof it
was missing: it had to be written into `dispatch_tool` because no seam had that
shape.

The other half is disposal. A plugin you cannot remove is a patch, and a system
that accumulates patches is one where nobody dares turn anything off — so every
registration hands back its own undo and the plugin author never writes teardown.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
from pydantic import BaseModel

from forge.plugins.context import MissingService, PluginContext, Services
from forge.plugins.loader import PluginError, load_plugin, load_plugins
from forge.plugins.waterfall import Bus, Waterfall


def _ctx(name: str = "p", services: Services | None = None,
         bus: Bus | None = None) -> PluginContext:
    return PluginContext(name, services or Services(), bus or Bus())


async def _core(*_args) -> str:
    return "core"


# ── the waterfall ────────────────────────────────────────────────────────────


def test_listeners_wrap_the_core_outermost_first():
    """Registration order is outermost-first. Stated in the docs because
    "first" is ambiguous for middleware, and a plugin author has to know which
    end of the onion they are on."""
    order: list[str] = []
    wf = Waterfall(event="e")

    async def outer(nxt):
        order.append("outer-in")
        result = await nxt()
        order.append("outer-out")
        return result

    async def inner(nxt):
        order.append("inner-in")
        result = await nxt()
        order.append("inner-out")
        return result

    wf.on("a", outer)
    wf.on("b", inner)
    assert asyncio.run(wf.run(_core)) == "core"
    assert order == ["outer-in", "inner-in", "inner-out", "outer-out"]


def test_a_listener_can_wrap_which_is_what_hooks_could_not_do():
    """The whole reason this exists. `pre_tool` + `post_tool` are two separate
    calls with nothing joining them, so no hook can hold state across the work
    — which is every timeout, every retry, every span."""
    wf = Waterfall(event="e")
    held = {}

    async def timing(nxt):
        held["armed"] = True
        try:
            return await nxt()
        finally:
            held["disarmed"] = True

    wf.on("timer", timing)
    asyncio.run(wf.run(_core))
    assert held == {"armed": True, "disarmed": True}


def test_a_listener_can_short_circuit():
    """Not calling `next()` answers without the work happening — how a cache
    responds and how a guard refuses."""
    wf = Waterfall(event="e")
    ran = []

    async def cached(_nxt):
        return "from-cache"

    async def core(*_a):
        ran.append(1)
        return "core"

    wf.on("cache", cached)
    assert asyncio.run(wf.run(core)) == "from-cache"
    assert not ran, "the core ran despite being short-circuited"


def test_a_listener_can_call_next_twice():
    """A retry plugin is only possible if `next()` is re-awaitable."""
    wf = Waterfall(event="e")
    calls = []

    async def retry(nxt):
        await nxt()
        return await nxt()

    async def core(*_a):
        calls.append(1)
        return len(calls)

    wf.on("retry", retry)
    assert asyncio.run(wf.run(core)) == 2
    assert len(calls) == 2


def test_each_layer_runs_its_own_listener():
    """Guards the late-binding bug: building the chain with a loop variable
    captured by reference makes every layer run the LAST listener, which
    presents as "my plugin ran three times and the others never did"."""
    wf = Waterfall(event="e")
    seen: list[str] = []

    def _make(tag):
        async def _fn(nxt):
            seen.append(tag)
            return await nxt()
        return _fn

    for tag in ("a", "b", "c"):
        wf.on(tag, _make(tag))
    asyncio.run(wf.run(_core))
    assert seen == ["a", "b", "c"]


def test_an_empty_bus_costs_nothing():
    bus = Bus()
    assert asyncio.run(bus.run("never/registered", _core)) == "core"


# ── disposal ─────────────────────────────────────────────────────────────────


def test_unloading_removes_every_registration():
    """The point of recording registrations rather than only performing them:
    the author writes no teardown, and therefore cannot write it wrong."""
    bus = Bus()
    services = Services()
    ctx = PluginContext("p", services, bus)

    async def listener(nxt):
        return await nxt()

    ctx.on("tools/execute", listener)
    ctx.provide("thing", object())

    class _T:
        name = "plugin_tool"

    ctx.tool(_T())

    assert len(bus.waterfall("tools/execute")) == 1
    assert services.has("thing") and ctx.tools()

    ctx.scope.unload()

    assert len(bus.waterfall("tools/execute")) == 0
    assert not services.has("thing")
    assert not ctx.tools()


def test_a_failing_disposer_does_not_strand_the_others():
    """Half-unloaded is strictly worse than fully unloaded."""
    ctx = _ctx()
    done = []
    ctx.on_unload(lambda: done.append("first"))
    ctx.on_unload(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx.on_unload(lambda: done.append("last"))

    ctx.scope.unload()
    assert done == ["last", "first"], "reverse order, and the throw was absorbed"


def test_unloading_twice_is_not_an_error():
    ctx = _ctx()
    ctx.on("e", lambda nxt: nxt())
    ctx.scope.unload()
    ctx.scope.unload()


# ── the loader ───────────────────────────────────────────────────────────────


def _install(mod_name: str, **attrs) -> str:
    """Put a throwaway plugin module on sys.path for one test."""
    module = types.ModuleType(mod_name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[mod_name] = module
    return mod_name


def test_a_plugin_is_name_config_and_apply():
    applied = {}

    def apply(ctx, config):
        applied["config"] = config
        applied["plugin"] = ctx.plugin

    spec = _install("_fp_basic", name="basic", apply=apply)
    loaded = load_plugin(spec, {}, Services(), Bus())

    assert loaded.name == "basic"
    assert applied["plugin"] == "basic"


def test_config_fails_loud_on_a_bad_value():
    """DSH's rule, and the right one: a silent default discards the operator's
    intent without telling them. They set something meaning something, got the
    default, and have no way to find out."""
    class Config(BaseModel):
        threshold: int

    spec = _install("_fp_cfg", name="cfg", Config=Config, apply=lambda ctx, c: None)

    with pytest.raises(PluginError, match="bad configuration"):
        load_plugin(spec, {"threshold": "not a number"}, Services(), Bus())


def test_a_missing_injected_service_fails_at_load_not_at_call():
    """A plugin that discovers its missing dependency mid-job has already
    changed the toolset the model was told about."""
    spec = _install("_fp_inj", name="inj", inject=("lsp",), apply=lambda ctx, c: None)

    with pytest.raises(PluginError, match="lsp"):
        load_plugin(spec, {}, Services(), Bus())


def test_a_plugin_that_throws_in_apply_is_fully_unloaded():
    """Half-applied is the worst of both: its listeners run, and its author
    believes it never loaded."""
    bus = Bus()

    def apply(ctx, config):
        ctx.on("tools/execute", lambda *a: None)
        raise RuntimeError("bad wiring")

    spec = _install("_fp_throw", name="thrower", apply=apply)
    with pytest.raises(PluginError, match="bad wiring"):
        load_plugin(spec, {}, Services(), bus)

    assert len(bus.waterfall("tools/execute")) == 0


def test_one_broken_plugin_does_not_stop_the_others():
    """A third-party plugin must not be able to take a deployment offline."""
    good = _install("_fp_good", name="good", apply=lambda ctx, c: None)
    bad = _install("_fp_bad", name="bad",
                   apply=lambda ctx, c: (_ for _ in ()).throw(RuntimeError("nope")))

    result = load_plugins([bad, good], Services())

    assert [p.name for p in result.loaded] == ["good"]
    assert "_fp_bad" in result.failed


def test_a_disabled_entry_is_skipped_without_importing():
    result = load_plugins(
        [{"plugin": "does.not.exist", "enabled": False}], Services())
    assert not result.loaded and not result.failed


def test_plugins_can_provide_services_to_each_other():
    """The composition DSH gets most of its power from: one plugin publishes,
    another consumes, and neither knows the other exists."""
    services = Services()
    bus = Bus()
    sentinel = object()

    provider = _install("_fp_prov", name="prov",
                        apply=lambda ctx, c: ctx.provide("widget", sentinel))
    consumer_saw = {}

    def consume(ctx, _c):
        consumer_saw["widget"] = ctx.require("widget")

    consumer = _install("_fp_cons", name="cons", inject=("widget",), apply=consume)

    result = load_plugins([provider, consumer], services, bus)
    assert len(result.loaded) == 2
    assert consumer_saw["widget"] is sentinel


def test_require_names_what_is_available_when_it_is_not():
    ctx = _ctx(services=Services(skills=object()))
    with pytest.raises(MissingService, match="skills"):
        ctx.require("lsp")
