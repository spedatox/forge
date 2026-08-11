"""`memory` — the owner's memory files, which live in Mark VI.

The peer receives the owner's memory every turn as a prompt fragment, and that
block ends by telling the agent to use "the `memory` tool" for everything it
does not contain: any project file, any person, the finance or training ledger,
and every write. Mark VI's in-process agents have that tool. This peer did not,
so the instruction named something that did not exist — and an agent told to
use a tool it has not got does not conclude the tool is missing. It writes as
though it had.

**The schema mirrors Mark VI's `MemorySkill` deliberately.** Same command names,
same argument names, same meaning. A model that has learned how memory works on
one engine must not have to learn it again because a different one is running
the turn — and the failure that would produce is silent: `file_text` renamed to
`content` here would be a validation error the model "fixes" by putting the
content somewhere it is dropped.

**No local rules about paths, ownership or routing.** Mark VI enforces those,
including which agent may write which document, and it is the only side that
can — it holds the schema, the revision trail and the custodian. A second
implementation here would be a second answer to the same question the first
time either side changed. So this validates almost nothing and passes the
command on.

Withheld entirely when there is no channel to Mark VI, on the same rule the
graph and vault tools follow: the standalone TUI has no backend, so the tool is
absent there rather than present and failing. `owner_memory.offline_fragment`
already tells that agent, in words, that memory is unreachable in this mode.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_NO_CHANNEL = (
    "There is no connection to Mark VI on this run, so the owner's memory "
    "cannot be read or written. This is the mode you are in, not a fault you "
    "can retry — say so plainly if it matters to the answer, and do not claim "
    "to have looked anything up or written anything down."
)

# One memory document is background, not the answer. Same cap and same reason as
# web_fetch and the vault: a long file must not evict the actual work.
MAX_CHARS = 20_000

_READ_COMMANDS = frozenset({"view"})


class MemoryArgs(BaseModel):
    command: str = Field(
        description="view: list a directory or read a file. create: create a new "
                    "file. str_replace: replace unique text in a file. insert: "
                    "insert text after a line number. delete: delete a file.")
    path: str = Field(
        description="File or directory path. Must start with /memories.")
    file_text: str | None = Field(
        default=None, description="File content, for the create command.")
    old_str: str | None = Field(
        default=None, description="Exact text to replace (must be unique in the file).")
    new_str: str | None = Field(default=None, description="Replacement text.")
    insert_line: int | None = Field(
        default=None, description="Line number to insert after (0 = before the first line).")
    insert_text: str | None = Field(default=None, description="Text to insert.")
    view_range: list[int] | None = Field(
        default=None, description="Optional [start_line, end_line] range for view.")


class Memory(Tool):
    name = "memory"
    description = (
        "Read or write the owner's persistent memory files under /memories. "
        "owner.md, current.md, dossier.md and history.md are ALREADY in your "
        "context every turn — never use this tool to read them. Use 'view' only "
        "to open a SPECIFIC other file when the task needs detail you do not "
        "already have: ONE project (/memories/projects/<name>.md), ONE person "
        "(/memories/social/<category>/<name>.md), finance.md, wellness.md, "
        "academic.md or log.md. The exact filenames are in the directory listing "
        "already in your context, so read the one entity you need rather than a "
        "whole folder — that is the entire reason projects and people are one "
        "file each. Use 'create'/'str_replace' only to FILE a genuinely new, "
        "durable fact in the ONE correct file per the routing rules in your "
        "memory protocol — a new person or project means 'create' on its own "
        "path, an active life state goes to current.md. Do not tidy other files; "
        "the custodian owns hygiene. Every write is versioned. Most turns need "
        "no memory operations at all.\n"
        "This is the owner's memory and not your notebook: it is shared with "
        "every other agent that serves him, and it outlives this conversation."
    )
    Args = MemoryArgs
    # Fail-closed on the flags, per command rather than for the class. A `view`
    # changes nothing and can share a batch; anything else writes to a store the
    # rest of the batch may also be writing to.
    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False

    def is_read_only(self, args: MemoryArgs) -> bool:
        return args.command in _READ_COMMANDS

    def is_concurrency_safe(self, args: MemoryArgs) -> bool:
        # Two reads cannot interfere. Two writes to one store can, and
        # str_replace in particular is read-modify-write on text a concurrent
        # call may have just moved.
        return args.command in _READ_COMMANDS

    async def call(self, args: MemoryArgs, ctx: ToolContext) -> ToolResult:
        channel = getattr(ctx, "memory", None)
        if channel is None:
            return ToolResult(_NO_CHANNEL, is_error=True)

        payload: dict[str, Any] = {
            k: v for k, v in args.model_dump().items() if v is not None
        }
        reply = await channel.command(payload)
        if not reply.ok:
            # An error, and said as one. A write that did not land must never
            # read to the model as a write that did.
            return ToolResult(reply.text or "The memory backend returned no answer.",
                              is_error=True)

        text = reply.text or ""
        if len(text) > MAX_CHARS:
            return ToolResult(
                f"{text[:MAX_CHARS]}\n\n[… truncated at {MAX_CHARS:,} of "
                f"{len(text):,} characters. This is the head of the file, not all "
                f"of it — do not conclude anything from what is missing.]")
        return ToolResult(text)
