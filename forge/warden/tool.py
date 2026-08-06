"""The tool boundary (§4).

A tool the model sees is exactly three things: `name`, `description`, and an
input schema (a Pydantic model → JSON Schema, the study's Zod→Pydantic mapping).
Everything else — read-only? concurrency-safe? destructive? result-size cap?
permissions? — is harness-side and invisible to the model.

Fail-closed defaults (§4): a new tool is assumed NOT concurrency-safe, NOT
read-only, NOT destructive, and NOT auto-permitted unless it declares otherwise.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from forge.cell.base import Cell
    from forge.graph.sidecar import GraphSidecar
    from forge.warden.permissions import Decision, PermissionEngine
    from forge.warden.filestate import FileStateCache
    from forge.warden.subagents import SubagentRunner
    from forge.warden.todos import TodoList


@dataclass
class ToolResult:
    """What a tool hands back to the loop. `is_error` is the uniform shape for
    every failure at every stage (§4) — the model reads it and adapts."""
    content: str
    is_error: bool = False
    display: str | None = None
    """An operator-facing rendering, never sent to the model.

    The same split the safety flags above use: `content` is the model's, this is
    the person's. It exists because the two audiences want opposite things from
    an edit — the model wrote the change and needs only "it applied", while the
    operator needs to SEE what landed in their file.

    Putting a diff in `content` would pay for it in context on every subsequent
    turn to tell the model something it already knows. Rendering it only in the
    TUI would break the rule that the TUI sees exactly what Mark VI sees. So it
    rides the tool_result event, where both surfaces can render it and neither
    the transcript nor the token bill carries it.
    """


@dataclass
class ToolContext:
    """Harness-side execution context. Never serialized to the model."""
    agent_id: str
    cell: "Cell"
    graph: "GraphSidecar | None"
    files: "FileStateCache"
    permissions: "PermissionEngine"
    network_allowed: bool
    todos: "TodoList | None" = None
    """The run's plan. Optional so an embedder that never wired one still
    constructs a context; todo_write reports its absence rather than raising."""

    subagents: "SubagentRunner | None" = None
    """How `task` spawns a child Warden. Injected rather than imported because
    only the embedder holds the model and the tool set — keeping it here means
    the tool boundary stays name/description/schema and nothing about spawning
    leaks into it. Optional on the same terms as `todos`: absent, the tool says
    so and the model does the work itself."""
    oracle: "Any | None" = None
    """Seam 2: who answers a gated action. None means nobody is reachable, which
    resolves to deny — the failure direction is fixed."""
    hooks: list = field(default_factory=list)
    """Seam 3 extension points, consulted inside the dispatch gauntlet. Empty
    unless something registered one at assembly."""


class Tool(abc.ABC):
    # ── Model-facing (the entire contract the model sees) ────────────────────
    name: str
    description: str
    Args: type[BaseModel]            # the input schema

    display_name: str = ""
    """What the operator sees in a transcript, e.g. `Read` for `read_file`.

    Separate from `name` because the two are read by different audiences for
    different reasons. The model needs a wire identifier that is stable and
    unambiguous; a person scanning what just happened needs a short verb. Empty
    means "derive it" (see `label`), so a tool only sets this when the
    derivation would be wrong.
    """

    @classmethod
    def label(cls) -> str:
        """The display name, derived from the wire name when not declared.

        `read_file` → `Read`, `run_command` → `Run`, `web_search` → `WebSearch`.
        The trailing noun goes because the argument already says what is being
        read or run, and `Read(calc.py)` carries more in less space than
        `read_file  calc.py`."""
        if cls.display_name:
            return cls.display_name
        parts = cls.name.split("_")
        if len(parts) > 1 and parts[-1] in ("file", "command"):
            parts = parts[:-1]
        return "".join(p.capitalize() for p in parts)

    # ── Harness-side, fail-closed defaults (§4) ──────────────────────────────
    # Declared as constants because most tools have one honest answer for every
    # input. Read through the methods below, never directly: a tool whose answer
    # depends on its arguments — a shell that is read-only for `ls` and not for
    # `rm` — overrides the method, and every call site must reach that override.
    READ_ONLY: bool = False          # assume it writes
    CONCURRENCY_SAFE: bool = False   # assume unsafe to parallelize
    DESTRUCTIVE: bool = False        # assume reversible; destructive tools opt in

    max_result_chars: float = 20_000  # cap one result; oversize is truncated/spilled

    def is_read_only(self, args: BaseModel) -> bool:
        return self.READ_ONLY

    def is_concurrency_safe(self, args: BaseModel) -> bool:
        """Whether THIS call may run alongside others. Per-input, because the
        answer usually is: two greps are safe together, two `pip install`s are
        not, and a tool that must answer for its worst case serializes its best
        one."""
        return self.CONCURRENCY_SAFE

    def is_destructive(self, args: BaseModel) -> bool:
        return self.DESTRUCTIVE

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a subclass that shadows a safety method with a plain value.

        These were class attributes before they were methods, so `is_read_only =
        True` still *looks* right. It silently replaces the method with a bool,
        every call site's `flag(args)` raises, and each one fails closed — the
        tool keeps working while quietly losing parallelism or gaining a gate.
        Failing closed is what makes this invisible, so it has to be caught here
        rather than discovered as a mysterious slowdown."""
        super().__init_subclass__(**kwargs)
        for name, constant in (("is_read_only", "READ_ONLY"),
                               ("is_concurrency_safe", "CONCURRENCY_SAFE"),
                               ("is_destructive", "DESTRUCTIVE")):
            value = cls.__dict__.get(name)
            if value is not None and not callable(value):
                raise TypeError(
                    f"{cls.__name__}.{name} is set to {value!r}, but it is a method. "
                    f"Declare `{constant} = {value!r}` instead, or override "
                    f"`{name}(self, args)` if the answer depends on the input.")

    @abc.abstractmethod
    async def call(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Do the work. May raise — the dispatcher converts any throw into an
        is_error result, so an exception never escapes the loop (§4)."""

    def check_permissions(self, args: BaseModel, ctx: ToolContext) -> "Decision | None":
        """Tool-specific permission opinion (e.g. shell subcommand rules).
        None = defer to the general precedence chain. Overridden by tools that
        need finer control than their default classification provides."""
        return None

    def schema(self) -> dict[str, Any]:
        """The model-facing tool definition — name, description, input schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.Args.model_json_schema(),
        }
