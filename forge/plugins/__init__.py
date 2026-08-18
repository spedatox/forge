"""Forge plugins — DSH's architecture, in Python, on Forge's seams.

A plugin is a module:

    from pydantic import BaseModel

    name = "redactor"
    inject = ()                       # services required at load

    class Config(BaseModel):
        patterns: list[str] = []

    def apply(ctx, config):
        async def scrub(tool, args, tool_ctx, next):
            result = await next()
            return replace(result, content=strip(result.content, config.patterns))
        ctx.on("tools/execute", scrub)

That is the whole contract. `name` identifies it, `Config` is validated before
`apply` runs and fails loud on a bad value, `inject` is checked against the
deployment's services at load, and `apply` registers everything through `ctx` so
that unloading needs no teardown code.

## Why this and not the hook seam

`warden/hooks.py` already offered `pre_tool` and `post_tool`. They are two
separate calls, so a plugin can inspect, veto or rewrite — but it cannot *wrap*.
A timeout has to arm a clock, call through, and disarm it; a retry has to call
through twice; a cache has to be able to not call through at all. None of those
decompose into a before and an after.

Forge's own tool deadline is the evidence: it is written into `dispatch_tool`
because there was no seam shaped like it. DSH ships the same behaviour as
`dsh-tool-call-timeout-policy`, an ordinary plugin, and the only reason it can
is `next()`. The hooks remain — they are simpler and most plugins want them —
but the waterfall is what makes the system able to grow behaviour it was not
designed for, which is the actual test of a plugin architecture.

## The events

Registration order is outermost-first: the first listener registered sees the
call first and the result last.

| event | signature | fires |
|---|---|---|
| `tools/execute` | `(tool, args, ctx, next) -> ToolResult` | around every dispatched tool call, inside the gauntlet, after permission |
| `agent/turn` | `(state, next) -> None` | around each model turn |
| `session/start` | `(session, next) -> None` | once, before the first turn |

`tools/execute` sits *after* permission resolution and *before* result capping,
the same two boundaries `pre_tool` and `post_tool` sit on, and for the same
reasons: a plugin must not be able to observe — let alone approve — what the
gate refused, and a redactor must see the whole output rather than a preview.

The bus creates events on first use, so a plugin may define its own extension
point and another may listen on it without the core knowing either exists. The
table above is the vocabulary Forge itself fires; it is not a whitelist.

## Loading

Named in `.forge/extensions.json`, in order, never by import side effect:

    {
      "plugins": [
        "repeat-tool-reminder",
        {"plugin": "acme.redactor", "config": {"patterns": ["sk-[a-z]+"]}},
        {"plugin": "noisy-tracer", "enabled": false}
      ]
    }

A bare name resolves inside `forge.plugins.builtin`; anything dotted is imported
as written. One plugin failing to import, validate, or apply is logged and
skipped — a broken third-party plugin must not take a deployment offline.
"""
from forge.plugins.context import MissingService, PluginContext, Scope, Services
from forge.plugins.loader import (
    Loaded, PluginError, PluginSet, load_plugin, load_plugins)
from forge.plugins.waterfall import Bus, Listener, Next, Waterfall

__all__ = [
    "Bus", "Listener", "Loaded", "MissingService", "Next", "PluginContext",
    "PluginError", "PluginSet", "Scope", "Services", "Waterfall",
    "load_plugin", "load_plugins",
]
