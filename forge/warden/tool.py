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


# Generous on purpose. This number is not a judgement about how long work should
# take; it is the line past which a tool is presumed wedged rather than busy.
# Set it tight and it becomes a second, worse timeout competing with the real
# ones — a `run_command` cut off at 60s by the backstop instead of by the Cell
# reports the wrong cause and teaches the model to distrust its own commands.
#
# 420 rather than a rounder 300 to stay off the numbers operators actually
# configure — a shipped profile in this repo sets its Cell to exactly 300, and
# two different clocks both reading 300 produce a timeout nobody can attribute
# from the message. A backstop that cannot be told apart from the bound it backs
# up is a debugging tax every time it fires.
DEFAULT_TOOL_TIMEOUT_S: float = 420.0

SELF_BOUNDED: float = float("inf")
"""For a tool whose own stopping condition is the only correct one.

The sentinel exists so opting out is a visible, greppable act rather than a
large number that looks like a considered value and is not — the same reason
`results.EXEMPT` exists next to `max_result_chars`."""

# How far a backstop sits above the inner bound it is backing up. Wide enough to
# cover a `docker exec` slow to hand back a result it already has: the inner
# timeout has fired, and the last thing wanted is the backstop racing it and
# blaming the harness for a command that was stopped correctly.
BACKSTOP_GRACE_S = 60.0


def cell_backed_timeout(ctx: "ToolContext", runs: int = 1,
                        per_run: float | None = None) -> float:
    """Backstop for a tool that does its work through `ctx.cell.run`.

    Every such tool is really bounded by the Cell, and the Cell's ceiling is
    per-profile — one agent's may be 120s and another's 300. A tool that
    inherited the flat `DEFAULT_TOOL_TIMEOUT_S` would therefore be correct under
    one profile and inverted under another, and under any profile that happens
    to name the same number as the default it would be an exact tie between two
    clocks with nothing deciding which wins.

    `runs` is the number of Cell calls the tool may make in sequence. It is not
    padding: `diagnostics` tries each checker in turn until one answers, so a
    workspace with no checker installed pays the full per-run budget several
    times over, and a backstop sized for one of them cancels the tool halfway
    through its normal fallback chain.

    `per_run` overrides the profile default for a tool that passes an explicit
    timeout to the Cell instead of inheriting one."""
    policy = getattr(getattr(ctx, "cell", None), "policy", None)
    if policy is None:
        return DEFAULT_TOOL_TIMEOUT_S
    per = policy.default_timeout_s if per_run is None else per_run
    return max(1, runs) * min(per, policy.max_timeout_s) + BACKSTOP_GRACE_S


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
    memory: "Any | None" = None
    """The owner's memory, which lives in Mark VI and is reached over the peer
    socket. None means there is no backend on this run — the standalone TUI —
    and the `memory` tool is withheld rather than offered and failing. Injected
    for the same reason `oracle` is: only the embedder holds the connection, and
    the tool boundary stays name/description/schema."""
    hooks: list = field(default_factory=list)
    """Seam 3 extension points, consulted inside the dispatch gauntlet. Empty
    unless something registered one at assembly."""

    bus: "Any | None" = None
    """The plugin waterfall bus, when a deployment loaded any.

    None means no plugins, and `dispatch_tool` then skips the chain entirely
    rather than composing an empty one — the cost of the plugin system on a
    deployment that uses none is a single `is None` per tool call.

    Carried on the ToolContext rather than passed to `dispatch_tool` because
    every seam a plugin can reach is already reachable from here, and adding a
    second channel would mean two answers to "what can a plugin see"."""

    on_command_output: "Any | None" = None
    """(stream, text) as a running command produces it, for a watching operator.

    Not on the emit channel, and the distinction matters: everything there is
    part of the job record that Mark VI replays and the transcript is built
    from, while a build's console output is a person looking over the agent's
    shoulder. It is also never seen by the model — the tool result is still the
    only thing that reaches the transcript, so what the operator watches cannot
    change what the agent concludes.

    None in headless mode, where there is no audience: the backends skip the
    callback entirely rather than rendering into nothing."""


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

    TIMEOUT_S: float = DEFAULT_TOOL_TIMEOUT_S
    """Wall clock this tool's `call` gets before the dispatcher cancels it.

    Declared here rather than enforced by each tool for the reason DSH's
    timeout-policy gives for the same choice: a budget the tool declares and a
    central listener enforces cannot be attached to a tool that does not exist,
    and cannot be forgotten by a tool that does. A new tool inherits a bound by
    existing, which is the same fail-closed direction as the three flags above.

    Most tools here already bound themselves — the Cell clamps `run_command`,
    httpx bounds `web_fetch`, the sidecar bounds `graph_*`, `CALL_TIMEOUT_S`
    bounds MCP. This is not a replacement for any of those and should never be
    the deadline that fires in normal operation. It is the backstop for the case
    those cannot cover: an inner timeout that a bug fails to arm, an await that
    never resolves because a socket died without closing, a tool written next
    year by someone who did not read this file. Every one of those hangs the
    whole loop, because `dispatch_tool` is awaited by the engine, and the only
    exit is the operator noticing and interrupting.

    Set `SELF_BOUNDED` to opt out, and only with a reason — see `task`."""

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

    def timeout_s(self, args: BaseModel, ctx: "ToolContext") -> float:
        """This call's wall clock. Takes `ctx` as well as `args` because the
        honest answer can depend on the world the call runs in — `run_command`
        reads its Cell's own ceiling so the backstop always sits above it."""
        return self.TIMEOUT_S

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
                               ("is_destructive", "DESTRUCTIVE"),
                               ("timeout_s", "TIMEOUT_S")):
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
