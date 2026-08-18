"""Finding plugins, validating their config, applying them, unloading them.

A Forge plugin is a **module** with up to four names, which is DSH's shape with
schemastery swapped for pydantic:

    name: str                       # required — identity, and the config key
    Config: type[BaseModel]         # optional — declared, validated, fails loud
    inject: tuple[str, ...]         # optional — services it cannot work without
    def apply(ctx, config) -> None  # required — the whole of registration

Nothing else is read, and nothing is discovered by importing: a plugin is loaded
because it is *named* in `.forge/extensions.json`, in the order written there.
Import side effects are how a plugin system becomes load-order-dependent in ways
nobody can see, and the fix — an explicit ordered manifest — costs one line per
plugin and is the difference between a bug you can read and a bug you bisect.

**Config fails loud on values, and is quiet about referents.** DSH draws this
line and it is the right one. An empty threshold list, a negative count, a
string where a number goes — these are mistakes with no sensible fallback, and a
silent default here means the operator's intent is discarded without anyone
being told. But a pattern matching nothing (`exclude: ["mcp_*"]` in a deployment
running no MCP servers) is *not* a mistake; it is a config that stays correct
across deployments. Validate what the value IS; do not validate what it points
at.

**One plugin's failure is one plugin's failure.** A plugin that will not import,
will not validate, or throws in `apply` is logged, unloaded, and skipped — the
rest load. The alternative is a single broken third-party plugin taking a
deployment offline, which is how operators learn not to install plugins.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from forge.plugins.context import MissingService, PluginContext, Services
from forge.plugins.waterfall import Bus

logger = logging.getLogger("forge.plugins")

#: Where a bare plugin name is looked up before being treated as a dotted path.
#: `"repeat-tool-reminder"` finds `forge.plugins.builtin.repeat_reminder`;
#: `"acme_plugins.redactor"` is imported as written. Two namespaces rather than
#: one so a shipped plugin can be named the way its config reads, and a
#: third-party one is never shadowed by a builtin added later.
BUILTIN_PACKAGE = "forge.plugins.builtin"


class PluginError(RuntimeError):
    """A plugin could not be loaded. Carries the plugin name for the log line."""


@dataclass
class Loaded:
    """One live plugin."""
    name: str
    module: Any
    ctx: PluginContext
    config: Any = None

    def unload(self) -> None:
        self.ctx.scope.unload()


@dataclass
class PluginSet:
    """Every plugin this deployment loaded, and the bus they share."""
    bus: Bus
    services: Services
    loaded: list[Loaded] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def tools(self) -> dict[str, Any]:
        """Tools contributed by plugins, in load order.

        Collisions between two plugins are refused here rather than silently
        resolved, on the same rule `toolsource` already applies to providers:
        the later source loses and the operator is told. A plugin that quietly
        replaced `write_file` would be a security incident wearing a feature's
        clothes."""
        out: dict[str, Any] = {}
        for entry in self.loaded:
            for name, tool in entry.ctx.tools().items():
                if name in out:
                    logger.warning("plugin_tool_collision",
                                   extra={"tool": name, "plugin": entry.name})
                    continue
                out[name] = tool
        return out

    def fragments(self) -> list[Any]:
        return [f for entry in self.loaded for f in entry.ctx.fragments()]

    def unload(self) -> None:
        """Reverse load order, so a provider outlives its consumers."""
        for entry in reversed(self.loaded):
            entry.unload()
        self.loaded.clear()

    def describe(self) -> list[str]:
        """One line per plugin, for `/plugins`."""
        listeners = self.bus.listeners()
        rows = []
        for entry in self.loaded:
            events = sorted(ev for ev, names in listeners.items()
                            if entry.name in names)
            tools = sorted(entry.ctx.tools())
            bits = []
            if events:
                bits.append("on " + ", ".join(events))
            if tools:
                bits.append("tools " + ", ".join(tools))
            rows.append(f"  {entry.name:24} {'; '.join(bits) or '(loaded)'}")
        for name, why in self.failed.items():
            rows.append(f"  {name:24} FAILED — {why}")
        return rows


def _import(spec: str) -> Any:
    """Resolve a manifest entry to a module.

    A bare name is a builtin; anything with a dot is an import path. Tried in
    that order so `.forge/extensions.json` reads as a list of capabilities
    rather than a list of Python paths, while a third-party plugin remains
    addressable without Forge having to know about it."""
    if "." not in spec:
        candidate = f"{BUILTIN_PACKAGE}.{spec.replace('-', '_')}"
        try:
            return importlib.import_module(candidate)
        except ModuleNotFoundError as e:
            if e.name != candidate:
                raise      # the plugin imported something missing — its problem, say so
    return importlib.import_module(spec)


def _validate(module: Any, name: str, raw: dict[str, Any]) -> Any:
    """Build the plugin's config object, or raise with the operator's mistake.

    A plugin with no `Config` gets the raw dict — plenty for something with two
    switches, and not worth a schema. A plugin WITH one gets pydantic's message
    reshaped: the raw text names `__root__` and `value_error.missing`, which
    tells an operator editing JSON almost nothing about which line is wrong."""
    schema = getattr(module, "Config", None)
    if schema is None:
        return raw
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        raise PluginError(f"{name}: Config must be a pydantic BaseModel subclass")
    try:
        return schema.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in e.errors())
        raise PluginError(f"{name}: bad configuration — {problems}") from e


def load_plugin(spec: str, raw_config: dict[str, Any],
                services: Services, bus: Bus) -> Loaded:
    """Import, validate, apply. Raises `PluginError` with a legible reason."""
    try:
        module = _import(spec)
    except Exception as e:  # noqa: BLE001 — any import fault is this plugin's
        raise PluginError(f"{spec}: could not import — {type(e).__name__}: {e}") from e

    name = getattr(module, "name", None) or spec
    if not callable(getattr(module, "apply", None)):
        raise PluginError(f"{name}: no apply(ctx, config) function")

    for required in getattr(module, "inject", ()):  # declared dependencies
        if not services.has(required):
            raise PluginError(
                f"{name}: needs the {required!r} service, which this deployment "
                f"does not provide (has: {', '.join(services.names()) or 'none'})")

    config = _validate(module, name, raw_config)
    ctx = PluginContext(plugin=name, services=services, bus=bus)
    try:
        module.apply(ctx, config)
    except MissingService as e:
        ctx.scope.unload()
        raise PluginError(f"{name}: {e}") from e
    except Exception as e:  # noqa: BLE001 — a plugin that threw is not loaded
        # Unload first: `apply` may have registered several things before
        # throwing, and a half-applied plugin is the worst of both — its
        # listeners run but its author believes it never loaded.
        ctx.scope.unload()
        raise PluginError(f"{name}: apply() raised {type(e).__name__}: {e}") from e

    return Loaded(name=name, module=module, ctx=ctx, config=config)


def load_plugins(manifest: list[Any], services: Services,
                 bus: Bus | None = None) -> PluginSet:
    """Load every plugin in the manifest, skipping the ones that fail.

    A manifest entry is either a bare string (no config) or an object with
    `plugin` and optional `config` / `enabled`:

        "plugins": [
          "repeat-tool-reminder",
          {"plugin": "acme.redactor", "config": {"patterns": ["sk-[a-z]+"]}},
          {"plugin": "noisy-tracer", "enabled": false}
        ]

    `enabled: false` rather than deleting the entry, because the reason a plugin
    is off is usually worth keeping next to it."""
    bus = bus if bus is not None else Bus()
    result = PluginSet(bus=bus, services=services)

    for entry in manifest or []:
        if isinstance(entry, str):
            spec, raw, enabled = entry, {}, True
        elif isinstance(entry, dict):
            spec = str(entry.get("plugin") or entry.get("name") or "")
            raw = entry.get("config") or {}
            enabled = bool(entry.get("enabled", True))
        else:
            logger.warning("plugin_entry_invalid", extra={"entry": repr(entry)[:120]})
            continue

        if not spec:
            logger.warning("plugin_entry_unnamed")
            continue
        if not enabled:
            logger.info("plugin_disabled", extra={"plugin": spec})
            continue
        if not isinstance(raw, dict):
            result.failed[spec] = "config must be an object"
            logger.warning("plugin_config_not_object", extra={"plugin": spec})
            continue

        try:
            loaded = load_plugin(spec, raw, services, bus)
        except PluginError as e:
            result.failed[spec] = str(e)
            logger.warning("plugin_load_failed", extra={"plugin": spec, "error": str(e)})
            continue
        result.loaded.append(loaded)
        logger.info("plugin_loaded", extra={"plugin": loaded.name})

    return result
