"""The `ctx` a plugin is handed, and the scope that undoes what it did.

DSH's plugins receive a Cordis `Context`: it carries services, it is how you
register listeners, and it is *scoped*, so unloading a plugin removes everything
that plugin added without anyone tracking what that was. The third property is
the one that makes a plugin system safe to use twice — a plugin you cannot
remove is a patch, and a system that accumulates patches is one where nobody
dares turn anything off.

Three pieces here:

- **`Services`** — what a plugin may reach. A flat named registry, because the
  set is small and known, and because `inject` has to be checkable against it by
  name before `apply` runs.
- **`Scope`** — a disposal ledger. Everything a plugin registers hands back a
  disposer; the scope holds them and `unload()` runs them in reverse.
- **`PluginContext`** — the two joined, plus the registration verbs.

**Registration is recorded, not just performed.** `ctx.tool(...)` both adds the
tool and remembers how to remove it. That redundancy is the entire feature: it
means a plugin author never writes teardown, and therefore never writes it
wrong. Teardown that has to be maintained in parallel with setup is teardown
that is already out of date.

**A plugin cannot reach the core except through here.** Not enforced — this is
Python and a determined plugin can import whatever it likes — but the context is
sized so that going around it is visibly going around it, which is the most a
single-operator harness needs. The line worth holding is that nothing in
`forge/warden/` imports anything from `forge/plugins/builtin/`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from forge.plugins.waterfall import Bus, Listener

logger = logging.getLogger("forge.plugins")


class MissingService(RuntimeError):
    """A plugin declared `inject` for something this deployment does not have.

    Raised at LOAD, not at call. A plugin that discovers its missing dependency
    halfway through a job has already changed the toolset the model was told
    about, and the failure surfaces as a tool that mysteriously errors rather
    than as a deployment that is misconfigured."""


class Services:
    """Named capabilities a plugin may ask for.

    Deliberately not the `ToolContext`: that is per-job and carries the Cell, the
    permission engine, and the file cache — everything a *tool* needs while
    running. This is the assembly-time set, which is what a *plugin* needs while
    registering. Conflating them was tempting and wrong: it would mean a plugin
    could only be loaded once a job existed, which is exactly backwards.
    """

    def __init__(self, **initial: Any) -> None:
        self._services: dict[str, Any] = {k: v for k, v in initial.items() if v is not None}

    def get(self, name: str) -> Any:
        return self._services.get(name)

    def set(self, name: str, value: Any) -> Callable[[], None]:
        """Publish a service. Returns its disposer, so a plugin can provide one.

        A plugin providing a service for other plugins is the composition
        DSH gets most of its power from — `dsh-lsp` publishes `ctx.lsp` and
        `dsh-tool-lsp` consumes it, and neither knows the other exists."""
        previous = self._services.get(name)
        had = name in self._services
        self._services[name] = value

        def _dispose() -> None:
            if had:
                self._services[name] = previous
            else:
                self._services.pop(name, None)
        return _dispose

    def has(self, name: str) -> bool:
        return name in self._services

    def names(self) -> list[str]:
        return sorted(self._services)


@dataclass
class Scope:
    """Everything one plugin added, and how to take it back."""

    plugin: str
    _disposers: list[Callable[[], None]] = field(default_factory=list)

    def add(self, dispose: Callable[[], None]) -> None:
        self._disposers.append(dispose)

    def unload(self) -> None:
        """Undo the plugin, newest registration first.

        Reverse order because registrations can depend on each other — a plugin
        that publishes a service and then registers a listener using it must
        lose the listener before the service, or the listener runs once more
        against a service that is already gone.

        A disposer that raises is logged and the rest still run. Half-unloaded
        is strictly worse than fully unloaded, and one broken teardown must not
        strand the others."""
        while self._disposers:
            dispose = self._disposers.pop()
            try:
                dispose()
            except Exception as e:  # noqa: BLE001 — see docstring
                logger.warning("plugin_dispose_failed",
                               extra={"plugin": self.plugin, "error": repr(e)})


class PluginContext:
    """What `apply(ctx, config)` receives.

    One instance per plugin, sharing the deployment's `Services` and `Bus` but
    owning its own `Scope`. Sharing the first two is what lets plugins see each
    other; owning the third is what lets them be removed independently.
    """

    def __init__(self, plugin: str, services: Services, bus: Bus) -> None:
        self.plugin = plugin
        self.services = services
        self.bus = bus
        self.scope = Scope(plugin=plugin)
        self._tools: dict[str, Any] = {}
        self._fragments: list[Any] = []

    # ── reaching other plugins' work ─────────────────────────────────────────
    def require(self, name: str) -> Any:
        """A service this plugin declared and cannot work without."""
        value = self.services.get(name)
        if value is None:
            raise MissingService(
                f"plugin {self.plugin!r} needs the {name!r} service, which this "
                f"deployment does not provide. Available: "
                f"{', '.join(self.services.names()) or '(none)'}.")
        return value

    def optional(self, name: str) -> Any:
        """A service this plugin can do without. None when absent.

        The distinction matters for the same reason `graph_query` is withheld
        when there is no sidecar: a plugin that degrades is better than one that
        refuses to load, but only if it degrades deliberately."""
        return self.services.get(name)

    def provide(self, name: str, value: Any) -> None:
        """Publish a service for other plugins."""
        self.scope.add(self.services.set(name, value))

    # ── the registration verbs ───────────────────────────────────────────────
    def on(self, event: str, fn: Listener) -> None:
        """Attach an around-listener. See `waterfall.py` for the contract."""
        self.scope.add(self.bus.on(event, self.plugin, fn))

    def tool(self, tool: Any) -> None:
        """Contribute a tool. Collision is refused at fold time, loudly."""
        name = getattr(tool, "name", None)
        if not name:
            raise ValueError(f"plugin {self.plugin!r} registered a tool with no name")
        self._tools[name] = tool
        self.scope.add(lambda: self._tools.pop(name, None))

    def prompt_fragment(self, fragment: Any) -> None:
        """Contribute a system-prompt section (Seam 7)."""
        self._fragments.append(fragment)
        self.scope.add(
            lambda: self._fragments.remove(fragment)
            if fragment in self._fragments else None)

    def on_unload(self, fn: Callable[[], None]) -> None:
        """Anything the verbs above do not cover — a socket, a subprocess, a
        watcher. The escape hatch exists so a plugin never has to leave
        something running because the context had no word for it."""
        self.scope.add(fn)

    # ── what the loader collects ─────────────────────────────────────────────
    def tools(self) -> dict[str, Any]:
        return dict(self._tools)

    def fragments(self) -> list[Any]:
        return list(self._fragments)
