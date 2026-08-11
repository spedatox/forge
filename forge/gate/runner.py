"""run_job — the single assembly point behind every Gate front door.

Given a validated JobRequest and an agent config, it builds a FRESH, UNSHARED Cell
(§9.1), starts a Graphify sidecar over the job's repo (§5), constructs the model
and tools from the agent's identity, runs the Warden loop, and streams JobEvents
as they happen. It always tears the Cell and sidecar down, even on failure.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from forge.agents import conventions, owner_memory
from forge.agents.config import AgentConfig
from forge.agents.prompt import PromptFragment, compose_system_prompt
from forge.agents.registry import AgentRegistry
from forge.cell.base import CellPolicy
from forge.cell.factory import build_cell
from forge.config import ForgeSettings
from forge import notify
from forge.gate.events import EventFan
from forge.gate.protocol import JobEvent, JobRequest
from forge.graph.sidecar import GraphSidecar
from forge.model.base import Model
from forge.warden import images
from forge.warden.engine import Warden
from forge.warden.toolsource import (
    BuiltinToolProvider,
    ToolProvider,
    close_providers,
    fold_providers,
    resolve_optional,
    without_graph_tools,
    without_memory_tools,
)
from forge.warden.filestate import FileStateCache
from forge.warden.todos import TodoList
from forge.warden.ledger import TokenLedger
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason, Terminal
from forge.warden.subagents import SubagentRunner
from forge.warden.tool import ToolContext
from forge.warden.transcript import repair_transcript

logger = logging.getLogger("forge.gate")

EmitEvent = Callable[[JobEvent], Awaitable[None]]


async def run_job(
    request: JobRequest,
    *,
    settings: ForgeSettings,
    registry: AgentRegistry,
    emit: EmitEvent,
    model: Model | None = None,
    signal: asyncio.Event | None = None,
    allowlist: AllowList | None = None,
    oracle: Any | None = None,
    tool_providers: list[ToolProvider] | None = None,
    hooks: list | None = None,
    fragments: list[PromptFragment] | None = None,
    event_sinks: list | None = None,
    memory: Any | None = None,
) -> Terminal:
    """Run one job to a single Terminal, streaming JobEvents via `emit`.

    `model` may be injected (the demo/tests pass a ScriptedModel); when None, the
    real model is built from the agent profile's model_ref."""
    cfg = registry.get(request.agent)
    signal = signal or asyncio.Event()

    # Seam 4: the transport is one sink among several, and no sink can fail the
    # job. Callers append journals, metrics, notifiers here.
    fan = EventFan([*(event_sinks or []), emit])

    async def out(etype: str, data=None) -> None:
        await fan(JobEvent(job_id=request.job_id, type=etype, data=data))

    await out("started", {"agent": cfg.agent_id, "job_id": request.job_id})

    # Resolve constraints over profile defaults (§7 overrides profile).
    c = request.constraints
    max_iterations = c.max_iterations or cfg.max_iterations
    allow_network = c.network or cfg.cell.allow_network
    repo_path = Path(request.repo_path).resolve() if request.repo_path else None

    policy = CellPolicy(
        allow_network=allow_network,
        cpus=cfg.cell.cpus,
        memory_mb=cfg.cell.memory_mb,
        default_timeout_s=c.timeout_s or cfg.cell.timeout_s,
        run_as_root=cfg.cell.run_as_root,
        cap_add=cfg.cell.cap_add,
        # A dispatched job commits under the agent's name for the same reason an
        # interactive one does — the history should record who wrote the code.
        env=cfg.git.env(),
    )

    cell = None
    graph = None
    providers: list[ToolProvider] = []
    try:
        cell = await build_cell(
            agent_id=cfg.agent_id,
            workspace_root=settings.workspace_root,
            backend=cfg.cell.backend or settings.cell_backend,
            image=cfg.cell.image or settings.cell_image,
            policy=policy,
            workspace=repo_path,
        )

        # Graphify sidecar over the job's repo — Warden-side, indexed once (§5).
        if repo_path is not None:
            graph = GraphSidecar(repo_path)
            await graph.start()
            await out("chunk", f"[graph: {'ready' if graph.available else 'unavailable'}]\n")

        # Heartbreaker's model picker overrides the profile's default for this
        # job; None means "use the profile's model_ref" (Rule 10: model IDs
        # live only in profiles, and the override is a profile-level concept).
        model_ref = request.model_override or cfg.model_ref
        # A turn carrying a photo goes to the profile's vision model instead —
        # Optimus is pinned to a text-only model and the picture would otherwise
        # reach a provider that cannot read it. An explicit override still wins
        # (README precedence: the job's model is highest), so a deliberate pick
        # of a blind model fails out loud rather than being quietly overruled.
        if (not request.model_override and cfg.vision_model
                and images.has_image(request.history)):
            model_ref = cfg.vision_model
            await out("chunk", f"[image in this turn — using {model_ref}]\n")
        # One number: what a turn may produce is also what the ledger holds back
        # for the compaction call. If these drifted apart, compaction would
        # trigger with either too little room to finish or more than it needs.
        model = model or _build_model(model_ref, settings, settings.max_tokens)

        # Seam 1: tools arrive by folding an ordered provider list. The builtin
        # set comes through the same door as anything else would.
        providers = list(tool_providers) if tool_providers else [BuiltinToolProvider()]

        # Graphify is optional and often absent at the start of a job. Offering
        # its query tools anyway costs a call every time the model takes their
        # advice to "use this FIRST to orient yourself".
        #
        # Availability is read at CALL time, not captured: `graph_index` can
        # build one mid-job, and the loop refreshes tools each iteration — so
        # the query tools appear on the next turn rather than staying withheld
        # for a job the agent has just made them useful for.
        # Built BEFORE the toolset, because the toolset now asks it whether a
        # graph is live.
        ctx = ToolContext(
            agent_id=cfg.agent_id,
            cell=cell,
            graph=graph,
            files=FileStateCache(),
            todos=TodoList(),
            permissions=PermissionEngine(
                mode=Mode(cfg.permission_mode),
                allowlist=allowlist or AllowList(),
            ),
            network_allowed=allow_network,
            oracle=oracle,                    # Seam 2
            memory=memory,                    # the owner's memory, in Mark VI
            hooks=list(hooks or []),          # Seam 3
        )

        async def _tools() -> dict:
            built = resolve_optional(await fold_providers(providers, cfg, request))
            # The owner's memory needs a live channel to Mark VI, and a job that
            # has none must not be offered the tool: the memory block in its
            # prompt already tells it to use one, and a tool that can only fail
            # turns that into a wasted call and a wrong conclusion.
            if ctx.memory is None:
                built = without_memory_tools(built)
            live = ctx.graph
            if live is not None and getattr(live, "available", False):
                return built
            return without_graph_tools(built)

        tools = await _tools()

        # Seam 7: the profile's identity is itself a fragment, so nothing has to
        # be reshaped when a second contributor appears.
        # A dispatched job works in the same repository an interactive one
        # does, so it gets the same conventions.
        repo_conventions = conventions.fragment(repo_path) if repo_path else None
        # What Mark VI knows about the owner. Cached to disk on the way past so
        # a standalone run still has it when the backend is unreachable — see
        # forge/agents/owner_memory.py on why that cache is read-only.
        owner_memory.remember(request.memory_block)
        owner_fragment = owner_memory.live_fragment(request.memory_block)
        system_prompt = compose_system_prompt([
            PromptFragment("profile", cfg.system_prompt),
            *([owner_fragment] if owner_fragment else []),
            *([repo_conventions] if repo_conventions else []),
            *(fragments or []),
        ])

        # One ledger for the whole job, parent and subagents alike, so a run's
        # reported cost is what the run cost rather than what its top-level
        # turns cost.
        ledger = TokenLedger(context_limit=settings.context_limit,
                             max_output_tokens=settings.max_tokens)
        emit = lambda ev: fan(JobEvent(job_id=request.job_id, type=ev["type"],  # noqa: E731
                                       data=ev.get("data")))

        warden = Warden(
            system_prompt=system_prompt,
            tools=tools,
            model=model,
            ctx=ctx,
            max_iterations=max_iterations,
            signal=signal,
            ledger=ledger,
            retry_attempts=settings.retry_attempts,
            retry_base_delay=settings.retry_base_delay_s,
            refresh_tools=_tools,      # keeps the graph filter on a mid-job refresh
            emit=emit,
        )

        # Seam 8: subagents. A child is the same engine with a different prompt
        # and a narrower toolset — it shares the Cell (same workspace), the
        # model, the interrupt signal and the ledger, and differs only in
        # having its own message list. That difference is the entire point.
        ctx.subagents = SubagentRunner(
            build_warden=lambda **kw: Warden(
                model=model, ctx=ctx, signal=signal, ledger=ledger,
                retry_attempts=settings.retry_attempts,
                retry_base_delay=settings.retry_base_delay_s,
                **kw,          # includes the child's scoped emit
            ),
            parent_tools=lambda: tools,
            emit=emit,
        )
        # A chat turn carries the full prior transcript — seed the loop with it so
        # the agent remembers the conversation. A bare dispatch has no history and
        # runs single-shot on `task`. The transcript is repaired first: a previous
        # turn that died mid-tool-call leaves a dangling tool_use the API rejects,
        # and replaying that raw would make the agent forget the whole
        # conversation instead of just the one botched turn.
        history = repair_transcript(request.history) if request.history else []
        started = time.monotonic()
        if history:
            terminal = await warden.run_messages(history)
        else:
            terminal = await warden.run(request.task)

        # Tell the owner, if they are not here to see it. A dispatched job runs
        # on a box nobody is logged into; without this its completion exists
        # only as a journal line. Awaited rather than fired-and-forgotten so it
        # cannot be cancelled by the teardown in `finally` — it is two HTTP
        # calls at most, and `send` never raises.
        await notify.job_finished(
            cfg.agent_id, request.task,
            (terminal.final_text or "").strip(),
            time.monotonic() - started,
            ok=terminal.reason is StopReason.COMPLETED,
        )
        return terminal
    except Exception as e:  # noqa: BLE001 — fail loud (§9.5) as a terminal error event
        logger.exception("run_job_failed")
        await out("error", f"{type(e).__name__}: {e}")
        return Terminal(reason=StopReason.ERROR, error=f"{type(e).__name__}: {e}")
    finally:
        await close_providers(providers)
        if graph is not None:
            await graph.close()
        if cell is not None:
            await cell.close()


def _build_model(model_ref: str, settings: ForgeSettings, max_tokens: int) -> Model:
    """Construct the model from a ``provider:model`` ref via the multi-provider
    factory (Anthropic / OpenAI / Gemini / z.ai / DeepSeek / Ollama).  The ref
    is either the agent profile's default or a per-job override from Heartbreaker's
    model picker.  A missing key for the selected provider fails loud."""
    from forge.model.factory import build_model
    return build_model(model_ref, settings, max_tokens=max_tokens)
