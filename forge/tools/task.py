"""The `task` tool — hand a job to a subagent and get back only the answer.

The cost this exists to avoid is context, not time. Finding one function in an
unfamiliar repo can take twenty file reads, and the parent then carries those
twenty files for the rest of the session, paying for them on every subsequent
turn. Delegating the search costs it one paragraph.

See forge/warden/subagents.py for the boundaries (depth one, allowlist-as-reach).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.subagents import BUILT_INS, catalogue, spec_for
from forge.warden.tool import Tool, ToolContext, ToolResult


class TaskArgs(BaseModel):
    description: str = Field(
        description="A short (3-5 word) description of the task, for the operator's display.")
    prompt: str = Field(
        description=(
            "The task for the subagent. It starts with a COMPLETELY fresh "
            "context and sees nothing of this conversation, so this must be "
            "self-contained: state the goal, name the paths or symbols "
            "involved, and say what a good answer looks like. A prompt that "
            "refers to 'the file we were just looking at' will fail."
        ))
    subagent_type: str = Field(
        default="general",
        description="Which specialist to use. One of: explore, review, general.")


class TaskTool(Tool):
    name = "task"
    display_name = "Task"
    description = (
        "Runs a task in a SEPARATE agent with its own context window, and "
        "returns only that agent's final report. Use it when the work would "
        "otherwise flood this conversation with material you do not need to "
        "keep — searching an unfamiliar codebase, reviewing a change, or any "
        "self-contained job whose intermediate steps do not matter to you. "
        "The subagent starts fresh and sees NOTHING of this conversation, so "
        "everything it needs must be in the prompt; it cannot ask you a "
        "follow-up question, and it cannot spawn subagents of its own. Do NOT "
        "use it for work you could finish in one or two tool calls, where it "
        "is pure overhead, or for anything that needs the context you are "
        "holding right now. Emit several calls in one turn to run them in "
        "parallel. Returns the subagent's final message as text.\n\n"
        "Available types:\n" + catalogue()
    )
    Args = TaskArgs

    # Spawning is not itself a mutation, but a `general` subagent's own calls
    # certainly can be. Each of those is permission-checked individually inside
    # the child loop; declaring this read-only would let the whole delegation
    # past a gate that never saw what it went on to do.
    READ_ONLY = False
    CONCURRENCY_SAFE = True    # several may run at once; SubagentRunner caps it
    DESTRUCTIVE = False

    async def call(self, args: TaskArgs, ctx: ToolContext) -> ToolResult:
        runner = getattr(ctx, "subagents", None)
        if runner is None:
            # Same posture as todo_write with no plan wired: report it, so the
            # model routes around it instead of retrying into a hard failure.
            return ToolResult(
                content=("Subagents are not available in this deployment. "
                         "Do this work yourself in the current context."),
                is_error=True,
            )

        spec = spec_for(args.subagent_type)
        if spec is None:
            return ToolResult(
                content=(f"There is no {args.subagent_type!r} subagent. "
                         f"Available: {', '.join(sorted(BUILT_INS))}."),
                is_error=True,
            )

        prompt = (args.prompt or "").strip()
        if not prompt:
            return ToolResult(
                content="A subagent needs a task. `prompt` was empty.",
                is_error=True,
            )

        # The operator's label for this delegation. It rides the subagent
        # events so a panel can title the run with what it is FOR, rather than
        # with a specialist's name and an opaque id.
        report, failed = await runner.run(spec, prompt, label=args.description)
        return ToolResult(content=report, is_error=failed,
                          display=f"{spec.name}: {args.description}")
