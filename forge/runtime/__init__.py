"""Identity-free Forge execution for orchestrators such as Mark VI.

The caller owns personas, history, model credentials, job persistence and user
delivery.  Forge owns one bounded coding/security loop and its isolated Cell.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Literal

from forge.agents.config import AgentConfig, CellSpec
from forge.agents.registry import AgentRegistry
from forge.config import ForgeSettings
from forge.gate.protocol import JobConstraints, JobEvent, JobRequest
from forge.gate.runner import run_job
from forge.model.base import Model
from forge.tools import CODING_TOOLS, SECURITY_TOOLS
from forge.warden.state import StopReason

Role = Literal["coder", "reviewer", "pentester"]
Emit = Callable[[JobEvent], Awaitable[None]]

_PROMPTS = {
    "coder": (
        "You are an anonymous Forge coding worker. Complete only the assigned "
        "task in the supplied workspace. Inspect before editing, keep changes "
        "scoped, run meaningful checks, and report files changed, checks run, "
        "assumptions, and unresolved limitations. You have no persona, owner "
        "memory, peer network, or authority to create another mission."
    ),
    "reviewer": (
        "You are an anonymous Forge code-review worker. Inspect the supplied "
        "workspace and task, prioritize concrete correctness and security "
        "findings, cite files and lines, and do not modify files. You have no "
        "persona, owner memory, peer network, or authority to delegate."
    ),
    "pentester": (
        "You are an anonymous Forge security-assessment worker. Work only on "
        "the explicitly supplied local workspace and authorized task. Prefer "
        "repeatable evidence, distinguish failed coverage from no finding, and "
        "report severity, evidence, remediation, and limitations. Network access "
        "is denied unless the trusted caller enables a scoped assessment."
    ),
}


@dataclass(frozen=True)
class ExecutionSpec:
    job_id: str
    role: Role
    task: str
    workspace: Path
    model_ref: str
    max_iterations: int = 30
    timeout_s: int = 120
    allow_network: bool = False
    cell_backend: str | None = None
    cell_image: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    job_id: str
    status: Literal["succeeded", "cancelled", "failed"]
    report: str
    error: str | None = None


async def execute(
    spec: ExecutionSpec,
    *,
    model: Model,
    emit: Emit,
    signal: asyncio.Event | None = None,
    settings: ForgeSettings | None = None,
) -> ExecutionResult:
    """Execute one anonymous job using an injected, orchestrator-owned model."""
    workspace = spec.workspace.resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist or is not a directory: {workspace}")

    tool_types = SECURITY_TOOLS if spec.role == "pentester" else CODING_TOOLS
    denied = {"task", "ask_operator", "claude_code", "enter_worktree", "exit_worktree"}
    tools = tuple(t.name for t in tool_types if t.name not in denied)
    if spec.role == "reviewer":
        tools = tuple(n for n in tools if n not in {"write_file", "edit_file", "run_command"})

    cfg = AgentConfig(
        agent_id=f"forge-{spec.role}",
        name="Forge worker",
        domain=spec.role,
        model_ref=spec.model_ref,
        tool_names=tools,
        system_prompt=_PROMPTS[spec.role],
        max_iterations=spec.max_iterations,
        cell=CellSpec(
            allow_network=spec.allow_network,
            timeout_s=spec.timeout_s,
            backend=spec.cell_backend,
            image=spec.cell_image,
        ),
    )
    request = JobRequest(
        agent=cfg.agent_id,
        task=spec.task,
        repo_path=str(workspace),
        job_id=spec.job_id,
        constraints=JobConstraints(
            timeout_s=spec.timeout_s,
            max_iterations=spec.max_iterations,
            network=spec.allow_network,
        ),
        unattended=True,
    )
    runtime_settings = settings or ForgeSettings.from_env()
    # The orchestrator-owned model adapter owns provider retry/fallback policy.
    # Retrying again in Warden would multiply calls and obscure accounting.
    runtime_settings = replace(runtime_settings, retry_attempts=0)
    terminal = await run_job(
        request,
        settings=runtime_settings,
        registry=AgentRegistry({cfg.agent_id: cfg}),
        emit=emit,
        model=model,
        signal=signal,
        identity_free=True,
    )
    if terminal.reason is StopReason.COMPLETED:
        status = "succeeded"
    elif terminal.reason is StopReason.ABORTED:
        status = "cancelled"
    else:
        status = "failed"
    return ExecutionResult(
        job_id=spec.job_id,
        status=status,
        report=(terminal.final_text or terminal.error or "").strip(),
        error=terminal.error,
    )


__all__ = ["ExecutionResult", "ExecutionSpec", "execute"]
