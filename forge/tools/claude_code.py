"""The `claude_code` tool — delegate to the real Claude Code CLI.

This bridges Forge's subagent system to the Claude Code CLI, routed through
the free-claude-code proxy so it uses the same provider keys the rest of the
system does. It spawns `fcc-claude` inside the Cell, where it has the same
workspace and toolchain as any other command, and streams its activity through
the subagent emit channel so Heartbreaker shows the delegation live.

Streaming note: the Cell contract (`Cell.run`) returns complete output, not a
line-by-line stream. Until that contract grows a streaming method, the subagent
panel shows "started" → "Working…" → "finished" rather than per-tool-call
granularity. The operator still sees that a delegation is in flight, how long
it took, and the full report — the same visibility every other subagent gets.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import uuid

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_MAX_TURNS = 50
"""Safety ceiling: the model can override this in the args, but omitting it
means 50, which is high enough for real work and low enough that a loop will
not burn through the budget before someone notices."""

FCC_TIMEOUT_S = 600
"""Per-invocation wall-clock ceiling. The Cell's own `max_timeout_s` is 600,
so this matches it exactly — a longer value would only get clamped."""

FCC_SERVER_URL_DEFAULT = "http://172.17.0.1:8082"
"""The Docker bridge gateway. A Cell on the default `docker0` bridge reaches
the host at this address. Overridable via CLAUDE_CODE_SERVER_URL in the
environment (the forge@.service unit loads it from the env file)."""


# ── Args ──────────────────────────────────────────────────────────────────────

class ClaudeCodeArgs(BaseModel):
    description: str = Field(
        description=(
            "A short (3-5 word) label for this delegation, shown in the "
            "Heartbreaker subagent panel title. The operator reads this to "
            "know what is happening — make it about the task, not the tool. "
            "Good: 'auth prototype', 'refactor DB layer'. Bad: 'claude code run'."
        ))
    prompt: str = Field(
        description=(
            "The self-contained task for Claude Code. It starts with a "
            "COMPLETELY fresh context and sees nothing of this conversation, "
            "so this must be self-contained: state the goal, name the paths "
            "or symbols involved, and say what a good answer looks like. "
            "A prompt that refers to 'the file we were just looking at' will "
            "produce nonsense. Claude Code has its own tool set inside the "
            "Cell — it reads, writes, edits and runs commands — so you do "
            "not need to describe HOW to do the work, only WHAT to produce."
        ))
    max_turns: int = Field(
        default=DEFAULT_MAX_TURNS,
        description=(
            "Maximum number of tool-calling turns Claude Code may take "
            f"before it is stopped. Default {DEFAULT_MAX_TURNS}. Raise it "
            "for genuinely large jobs; lower it for quick checks where a "
            "runaway loop would waste time."
        ))


# ── Tool ──────────────────────────────────────────────────────────────────────

class ClaudeCode(Tool):
    name = "claude_code"
    display_name = "Claude Code"
    description = (
        "Delegates a coding task to Claude Code, an autonomous CLI agent "
        "that works inside your sandbox with its own tools. Use this for "
        "complex multi-step prototyping, refactoring, or any task that "
        "benefits from an external agent's own tool-use loop — the kind of "
        "work where you would otherwise spend ten turns reading, editing, "
        "and testing yourself. The subagent panel in Heartbreaker shows live "
        "status while it works.\n\n"
        "Claude Code starts with a FRESH context and sees nothing of this "
        "conversation, so everything it needs must be in the prompt. It can "
        "read, write, edit, and run commands — it has the full workspace — "
        "so the prompt should describe the goal, not every step. Use `task` "
        "(general subagent) for simpler work where spawning an external CLI "
        "is overhead, and do NOT use this for a single file read you could "
        "do directly."
    )
    Args = ClaudeCodeArgs

    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False

    async def call(self, args: ClaudeCodeArgs, ctx: ToolContext) -> ToolResult:
        # ── Guard: is fcc-claude reachable? ───────────────────────────────
        fcc_bin = shutil.which("fcc-claude")
        if fcc_bin is None:
            return ToolResult(
                content=(
                    "fcc-claude is not installed in the Cell. Install it first:\n"
                    "  npm install -g @anthropic-ai/claude-code\n"
                    "  npm install -g free-claude-code\n"
                    "Or use `task` (general subagent) for this work instead."
                ),
                is_error=True,
            )

        # ── Build the command ─────────────────────────────────────────────
        fcc_server_url = os.environ.get(
            "CLAUDE_CODE_SERVER_URL", FCC_SERVER_URL_DEFAULT)

        # shlex.quote so a prompt with shell metacharacters arrives as one
        # argument rather than being split into a tree of redirections.
        # --print: non-interactive (no REPL — this is a subprocess, not a TTY).
        # --permission-mode accept-edits: auto-approve file edits so the
        #   subagent can work without a human, while still gating destructive
        #   operations. This is the same posture the `general` subagent has —
        #   it can write files and run commands, and the Cell is the boundary.
        cmd = (
            f"{shlex.quote(fcc_bin)} "
            f"-p {shlex.quote(args.prompt)} "
            f"--output-format stream-json "
            f"--max-turns {max(1, args.max_turns)} "
            f"--print "
            f"--permission-mode accept-edits"
        )

        env = {
            "ANTHROPIC_BASE_URL": fcc_server_url,
            "ANTHROPIC_AUTH_TOKEN": "freecc",
            # fcc-claude finds the Anthropic CLI via PATH; the wrapper also
            # respects this env for the underlying CLI binary location.
            "CLAUDE_CODE_PATH": shutil.which("claude") or "",
        }

        # ── Subagent tracking ─────────────────────────────────────────────
        run_id = uuid.uuid4().hex[:12]
        emit = getattr(ctx, "subagents", None)
        base = {"id": run_id, "agent": "claude", "label": args.description}

        async def _emit(phase: str, **kw) -> None:
            if emit is not None and emit.emit is not None:
                try:
                    await emit.emit({
                        "type": "subagent",
                        "data": base | {"phase": phase, **kw},
                    })
                except Exception:
                    logger.debug("subagent_emit_failed", exc_info=True)

        # ── Run ───────────────────────────────────────────────────────────
        await _emit("started", prompt=args.prompt)

        # Run inside the Cell — same isolation as any other command. The
        # env dict merges with the Cell's policy env (git identity etc.).
        result = await ctx.cell.run(cmd, timeout=FCC_TIMEOUT_S, env=env)

        ok = result.exit_code == 0 and not result.timed_out

        # Build the report from whatever came back.
        if result.timed_out:
            report = (
                f"Claude Code timed out after {FCC_TIMEOUT_S}s. "
                "Partial output:\n\n" + (result.stdout or "(nothing)")
            )
        elif not ok:
            report = (
                f"Claude Code exited with code {result.exit_code}.\n\n"
                + (result.stderr or result.stdout or "(no output)")
            )
        else:
            report = result.stdout or "(no output)"

        await _emit("finished", ok=ok, report=report)

        return ToolResult(
            content=report,
            is_error=not ok,
            display=f"Claude Code: {args.description}",
        )
