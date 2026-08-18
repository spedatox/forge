"""The repeat guard, and the three blind spots it was written to close.

Forge already had repeat detection in `warden/reminders.py`. Every test here is
a case that one gets wrong, taken from DSH's `repeat-tool-reminder`, whose
README argues each of them.

It is also the proof that the plugin system carries real behaviour rather than
toy behaviour: this reaches nothing the core had to expose for it — one
`tools/execute` listener and the arguments already passing through.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel, ValidationError

from forge.plugins.builtin import repeat_reminder
from forge.plugins.context import PluginContext, Services
from forge.plugins.waterfall import Bus
from forge.warden.tool import ToolResult


class _Args(BaseModel):
    pattern: str = ""


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _loaded(**overrides):
    bus = Bus()
    ctx = PluginContext("repeat-tool-reminder", Services(), bus)
    repeat_reminder.apply(ctx, repeat_reminder.Config(**overrides))
    return bus


def _call(bus: Bus, tool: str, args: dict, ok: bool = True) -> str:
    async def core(*_a):
        return ToolResult("result" if ok else "failed", is_error=not ok)

    result = asyncio.run(
        bus.run("tools/execute", core, _Tool(tool), args, None))
    return result.content


# ── it counts successes, which the old rule did not ──────────────────────────


def test_a_repeated_SUCCEEDING_call_is_caught():
    """The old rule fired only on repeated failures and defended it: "a tool
    called twice the same way that worked twice is a loop doing its job." True
    of a grep. False of a model that has read the same file eight times, or
    re-run the same passing test after every edit."""
    bus = _loaded(thresholds=[3])
    assert "<system-reminder>" not in _call(bus, "read_file", {"path": "a.py"})
    assert "<system-reminder>" not in _call(bus, "read_file", {"path": "a.py"})
    third = _call(bus, "read_file", {"path": "a.py"})
    assert "<system-reminder>" in third
    assert "several times in a row" in third


def test_a_repeated_FAILING_call_is_caught_too():
    bus = _loaded(thresholds=[2])
    _call(bus, "grep", {"pattern": "x"}, ok=False)
    assert "<system-reminder>" in _call(bus, "grep", {"pattern": "x"}, ok=False)


# ── it cannot be laundered ───────────────────────────────────────────────────


def test_an_excluded_tool_neither_counts_nor_resets():
    """The old counter reset on ANY success in the batch, so a bookkeeping call
    between two identical failures laundered the loop. DSH names this exactly:
    tools interleaved into a loop must not launder it."""
    bus = _loaded(thresholds=[3], exclude=["todo_write"])
    _call(bus, "grep", {"pattern": "x"}, ok=False)
    _call(bus, "todo_write", {"items": []})           # transparent
    _call(bus, "grep", {"pattern": "x"}, ok=False)
    third = _call(bus, "grep", {"pattern": "x"}, ok=False)
    assert "<system-reminder>" in third, "the bookkeeping call laundered the chain"


def test_a_genuinely_different_call_resets_the_chain():
    """The other direction, and just as important: an agent that changed its
    arguments is adapting, not looping. Nagging it is how a reminder system
    becomes wallpaper."""
    bus = _loaded(thresholds=[3])
    _call(bus, "grep", {"pattern": "x"})
    _call(bus, "grep", {"pattern": "y"})       # different — resets
    _call(bus, "grep", {"pattern": "x"})
    assert "<system-reminder>" not in _call(bus, "grep", {"pattern": "x"})


def test_argument_order_is_not_identity():
    """A model that reorders its own arguments between attempts is still
    looping; comparing raw dicts would miss it."""
    bus = _loaded(thresholds=[2])
    _call(bus, "read_file", {"path": "a.py", "limit": 5})
    assert "<system-reminder>" in _call(bus, "read_file", {"limit": 5, "path": "a.py"})


# ── it escalates ─────────────────────────────────────────────────────────────


def test_the_first_threshold_is_gentle_and_later_ones_are_detailed():
    """The old rule fired once per job. Right for a judgement-shaped nudge;
    wrong for a counter, where the fifth occurrence is information the third
    did not carry."""
    bus = _loaded(thresholds=[2, 4])
    _call(bus, "grep", {"pattern": "x"})
    gentle = _call(bus, "grep", {"pattern": "x"})
    _call(bus, "grep", {"pattern": "x"})
    detailed = _call(bus, "grep", {"pattern": "x"})

    assert "several times in a row" in gentle
    assert "consecutive identical calls: 4" in detailed
    assert "grep" in detailed


def test_a_threshold_fires_once_per_run():
    bus = _loaded(thresholds=[2])
    _call(bus, "grep", {"pattern": "x"})
    assert "<system-reminder>" in _call(bus, "grep", {"pattern": "x"})
    assert "<system-reminder>" not in _call(bus, "grep", {"pattern": "x"})


# ── it advises and never blocks ──────────────────────────────────────────────


def test_the_call_still_runs_and_its_result_survives():
    """Advisory by construction. A legitimately repeated call is delayed by
    nothing and blocked by nothing — the harness can see the repetition but not
    whether it is pointless, so the decision stays with the model."""
    bus = _loaded(thresholds=[2])
    ran = []

    async def core(*_a):
        ran.append(1)
        return ToolResult("the real answer")

    for _ in range(2):
        out = asyncio.run(bus.run("tools/execute", core,
                                  _Tool("grep"), {"pattern": "x"}, None))
    assert len(ran) == 2, "the guard blocked a call it is only allowed to advise on"
    assert out.content.startswith("the real answer")


# ── config: loud on values, quiet on referents ───────────────────────────────


@pytest.mark.parametrize("bad", [[], [1], [3, 3]])
def test_a_nonsense_threshold_list_is_refused(bad):
    """Never a silent fall-back: an operator who set something meaning
    something must not be handed the default without being told."""
    with pytest.raises(ValidationError):
        repeat_reminder.Config(thresholds=bad)


def test_a_pattern_matching_no_live_tool_is_valid():
    """The other half of DSH's rule. `exclude: [mcp_*]` has to stay legal in a
    deployment that loads no MCP servers — validate what the value IS, not what
    it points at, or every config becomes deployment-specific."""
    cfg = repeat_reminder.Config(exclude=["mcp_*", "nothing_matches_this"])
    assert "mcp_*" in cfg.exclude


def test_a_large_payload_is_capped_in_the_reminder_but_not_in_the_key():
    """The cap bounds the reminder, never the detection: capping the key would
    make two different large writes look like a loop."""
    # Thresholds [2, 3] rather than [2]: the FIRST threshold is deliberately the
    # gentle form, which quotes no arguments at all, so the cap is only
    # observable from the second onward.
    bus = _loaded(thresholds=[2, 3], preview_chars=40)
    big = {"content": "x" * 5_000}
    _call(bus, "write_file", big)
    _call(bus, "write_file", big)
    out = _call(bus, "write_file", big)
    assert "more chars]" in out
    assert len(out) < 2_000, "the whole payload rode into the next request"

    fresh = _loaded(thresholds=[2], preview_chars=40)
    _call(fresh, "write_file", {"content": "a" * 5_000})
    assert "<system-reminder>" not in _call(
        fresh, "write_file", {"content": "b" * 5_000}), "two different writes were conflated"
