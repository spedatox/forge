"""Assembling the extension layer — the one place that reads config from disk.

Law 2 of MARK2_SEAMS says all seam wiring happens where jobs are assembled, and
nothing self-registers via import side effects. This is that place for
operator-supplied extensions: it reads a config file, builds providers and
fragments, and hands them to `run_job`. It said a future plugin loader would be
one more caller of the same functions; `forge/plugins/` is that loader and this
is where it is called, so the prediction held and the law still does. A plugin
is loaded because the manifest NAMES it, in the order written — importing a
plugin module never registers anything by itself.

The config is one JSON file, `.forge/extensions.json`:

    {
      "mcpServers": {
        "graphite": {"command": "npx", "args": ["-y", "@acme/graphite-mcp"],
                     "env": {"GRAPHITE_TOKEN": "..."}}
      },
      "skillsDirs": ["./.forge/skills"],
      "plugins": [
        "repeat-tool-reminder",
        {"plugin": "acme.redactor", "config": {"patterns": ["sk-[a-z]+"]}}
      ]
    }

Absent file means no extensions, which is the default posture and not an error.
Every failure here degrades to "that extension is not available" and is logged:
an operator's broken MCP config must not be able to stop an execution peer from
doing the work it was dispatched to do.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from forge.agents.prompt import PromptFragment
from forge.mcp.client import MCPServerSpec
from forge.mcp.provider import MCPToolProvider
from forge.skills.provider import SkillProvider
from forge.plugins.context import Services
from forge.plugins.loader import PluginSet, load_plugins
from forge.warden.toolsource import BuiltinToolProvider, ToolProvider

logger = logging.getLogger("forge.extensions")

DEFAULT_CONFIG = Path("./.forge/extensions.json")
DEFAULT_SKILLS_DIR = Path("./.forge/skills")


@dataclass
class Extensions:
    """What the operator's configuration contributed."""
    providers: list[ToolProvider] = field(default_factory=list)
    fragments: list[PromptFragment] = field(default_factory=list)
    hooks: list = field(default_factory=list)
    plugins: "PluginSet | None" = None
    """Loaded plugins and the bus they registered on. None when the manifest was
    empty, which is the default posture — and the reason `dispatch_tool` can
    skip the waterfall on an `is None` rather than composing an empty chain."""

    @property
    def bus(self):
        """The waterfall bus, or None. Handed to each job's ToolContext."""
        return self.plugins.bus if self.plugins is not None else None

    def unload(self) -> None:
        """Undo every plugin. Called where providers are closed, for the same
        reason: a process that loads plugins per run must be able to end one."""
        if self.plugins is not None:
            self.plugins.unload()

    def tool_providers(self) -> list[ToolProvider]:
        """Builtins first, so an extension can never shadow a core tool — the
        fold refuses collisions, and refusing means the *later* source loses."""
        return [BuiltinToolProvider(), *self.providers]


class PluginToolProvider:
    """Seam 1 adapter: tools contributed by plugins.

    A plugin's tools go through the same fold as builtins, skills and MCP —
    which is what makes the collision rule apply to them too. A plugin cannot
    shadow `write_file` by registering its own, because the fold refuses the
    later name and says so at startup. That guarantee is worth more than the
    convenience of letting a plugin override a core tool, and a plugin that
    genuinely needs to change one has `tools/execute` to wrap it with."""

    name = "plugins"

    def __init__(self, tools: dict) -> None:
        self._tools = tools

    async def provide(self, cfg, request) -> dict:
        # Plugin tools are fixed at load, so this is idempotent by construction
        # and safe to re-ask between turns.
        return dict(self._tools)

    async def close(self) -> None:
        return None


def _read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("extensions_config_unreadable",
                       extra={"path": str(path), "error": repr(e)})
        return {}
    return data if isinstance(data, dict) else {}


def _server_specs(config: dict) -> list[MCPServerSpec]:
    specs: list[MCPServerSpec] = []
    for name, entry in (config.get("mcpServers") or {}).items():
        if not isinstance(entry, dict) or not entry.get("command"):
            logger.warning("mcp_server_config_invalid", extra={"server": name})
            continue
        specs.append(MCPServerSpec(
            name=name,
            command=str(entry["command"]),
            args=tuple(str(a) for a in entry.get("args") or ()),
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        ))
    return specs


def load_extensions(config_path: Path = DEFAULT_CONFIG,
                    skills_dir: Path = DEFAULT_SKILLS_DIR) -> Extensions:
    """Build the extension layer from disk. Never raises."""
    config = _read_config(config_path)
    ext = Extensions()

    roots = [Path(p) for p in (config.get("skillsDirs") or [])] or [skills_dir]
    skills = SkillProvider.from_dirs(*roots)
    if skills.skills:
        ext.providers.append(skills)
        fragment = skills.fragment()
        if fragment is not None:
            ext.fragments.append(fragment)
        logger.info("skills_loaded", extra={"count": len(skills.skills)})

    for spec in _server_specs(config):
        ext.providers.append(MCPToolProvider(spec))

    # Plugins last, so a plugin's `inject` can name a service an earlier stage
    # published, and so a plugin tool can never shadow a builtin or an MCP tool
    # — the fold refuses collisions and the later source loses.
    manifest = config.get("plugins") or []
    if manifest:
        services = Services(skills=skills if skills.skills else None)
        ext.plugins = load_plugins(manifest, services)
        contributed = ext.plugins.tools()
        if contributed:
            ext.providers.append(PluginToolProvider(contributed))
            logger.info("plugin_tools", extra={"tools": sorted(contributed)})
        ext.fragments.extend(ext.plugins.fragments())
        if ext.plugins.failed:
            for spec, why in ext.plugins.failed.items():
                logger.warning("plugin_unavailable",
                               extra={"plugin": spec, "reason": why})

    return ext
