"""`todo_write` — the plan the model keeps for itself.

Cheap to call, and the cheapness is the point: a tool the model hesitates over
does not get used on the turn where it matters. It writes nothing, runs nothing,
and cannot fail in a way that costs work.

The one rule enforced mechanically is a single `in_progress` item. Everything
else about a plan is the model's business, but "what am I doing right now" has
exactly one answer, and a list with four things in flight is a list that has
stopped tracking anything. Enforcing it here rather than asking for it in the
prompt means it is true rather than usually true.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.todos import COMPLETED, IN_PROGRESS, MAX_ITEMS, STATUSES, Todo
from forge.warden.tool import Tool, ToolContext, ToolResult


class TodoItem(BaseModel):
    content: str = Field(description="The step, as a short imperative phrase: 'add the retry test'.")
    status: str = Field(description="One of: pending, in_progress, completed.")


class TodoWriteArgs(BaseModel):
    todos: list[TodoItem] = Field(
        description="The COMPLETE list as it should now stand. It replaces the "
                    "previous list — include finished items with status "
                    "'completed', do not send only the changes.")


class TodoWrite(Tool):
    name = "todo_write"
    description = (
        "Record and update your plan for the current job. Send the COMPLETE list "
        "every time — it replaces the previous one, so carry finished items "
        "forward marked 'completed' rather than dropping them. Use it for work "
        "with three or more distinct steps, and update it as you go: mark a step "
        "'in_progress' when you start it and 'completed' the moment it is done, "
        "rather than batching updates at the end. Exactly one item may be "
        "'in_progress'. Do NOT use it for a single-step task or to narrate "
        "something you have already finished — it is a working memory, not a "
        "report. The plan survives context compaction, so it is what keeps a long "
        "job coherent after earlier turns have been summarized away."
    )
    Args = TodoWriteArgs
    # It mutates harness state, so not read-only — but it touches no file and
    # runs no command, so plan mode has nothing to protect against and there is
    # nothing for the gate to weigh.
    READ_ONLY = False
    CONCURRENCY_SAFE = False   # two writes racing would silently lose one
    DESTRUCTIVE = False

    async def call(self, args: TodoWriteArgs, ctx: ToolContext) -> ToolResult:
        todos = getattr(ctx, "todos", None)
        if todos is None:
            return ToolResult(
                "This Forge has no plan store wired up — continue without it.",
                is_error=True,
            )

        if len(args.todos) > MAX_ITEMS:
            return ToolResult(
                f"Refused: {len(args.todos)} items is past the {MAX_ITEMS} cap. "
                "Collapse the detail — a plan the operator cannot read at a glance "
                "is not tracking anything.",
                is_error=True,
            )

        bad = [t.status for t in args.todos if t.status not in STATUSES]
        if bad:
            return ToolResult(
                f"Refused: unknown status {bad[0]!r}. Use one of: "
                f"{', '.join(STATUSES)}.",
                is_error=True,
            )

        active = [t for t in args.todos if t.status == IN_PROGRESS]
        if len(active) > 1:
            names = ", ".join(repr(t.content) for t in active[:3])
            return ToolResult(
                f"Refused: {len(active)} items marked in_progress ({names}). "
                "Exactly one step is current — mark the rest pending and come "
                "back to them.",
                is_error=True,
            )

        empty = [t for t in args.todos if not t.content.strip()]
        if empty:
            return ToolResult("Refused: an item has empty content.", is_error=True)

        todos.replace([Todo(content=t.content.strip(), status=t.status)
                       for t in args.todos])

        done, _, pending = todos.counts()
        if args.todos and not pending and not active:
            return ToolResult(f"{todos.render()}\n\nAll {done} steps complete.")
        return ToolResult(todos.render())
