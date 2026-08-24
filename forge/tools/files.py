"""File tools operating inside the Cell, with read-before-write grounding.

Freshness is tracked by content hash rather than mtime — cross-platform and
robust (the study notes mtime is unreliable on Windows / cloud-sync, so it falls
back to a content compare; the Forge uses the content compare as the primary
signal)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.diff import render_edit
from forge.warden.filestate import digest as _hash
from forge.warden.results import EXEMPT
from forge.warden.tool import Tool, ToolContext, ToolResult


# ── read_file ────────────────────────────────────────────────────────────────
DEFAULT_LINE_LIMIT = 2000
MAX_READ_CHARS = 100_000       # roughly 25k tokens: one read's share of a window

FILE_UNCHANGED_STUB = (
    "File unchanged since your last read. The contents from that earlier read are "
    "still current — refer to them rather than re-reading."
)


class ReadFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative path of the file to read.")
    offset: int | None = Field(
        default=None, ge=1,
        description="1-based line to start from. Omit to start at the beginning.")
    limit: int | None = Field(
        default=None, ge=1,
        description=f"How many lines to read. Defaults to {DEFAULT_LINE_LIMIT}.")


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from your sandbox workspace. Use this before editing a "
        "file — edits are rejected until you have read the current version "
        "(read-before-write). Output is line-numbered, which is what makes edit_file's "
        f"exact-match anchoring reliable and composes with grep's path:line output. Reads "
        f"up to {DEFAULT_LINE_LIMIT} lines by default; pass offset and limit to read a "
        "window of a larger file, so no file is ever too big to work with. A file under "
        "the default limit is read whole in one call — after a grep hit, check the "
        "file's size before windowing around its line number; a guessed offset/limit "
        "often costs a second call for content the first call already had. Read-only "
        "and safe to run in parallel."
    )
    Args = ReadFileArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True
    # Exempt from the result budget: spilling a read produces a file whose only
    # use is to be read back, so the tool bounds itself instead — see the ceiling
    # enforced in `call`, and offset/limit for working through a large file.
    max_result_chars = EXEMPT

    async def call(self, args: ReadFileArgs, ctx: ToolContext) -> ToolResult:
        try:
            content = await ctx.cell.read(args.path)
        except FileNotFoundError as e:
            return ToolResult(f"File not found: {args.path} ({e})", is_error=True)

        digest = _hash(content)
        windowed = args.offset is not None or args.limit is not None

        # A whole-file re-read of something unchanged is the single most common
        # duplicate in a long transcript. The model already has this text; hand
        # back a pointer instead of a second copy. Only valid when the earlier
        # read showed everything — after a windowed read the model does not.
        prior = ctx.files.get(args.path)
        if not windowed and prior is not None and prior.mtime == digest and prior.shown_fully:
            return ToolResult(FILE_UNCHANGED_STUB)

        # Freshness always records the hash of the WHOLE file, even for a window:
        # edit-grounding must cover the parts that were not shown, or an edit to
        # an unseen region would sail through on a partial read.
        ctx.files.record(args.path, content, digest, shown_fully=not windowed)

        lines = content.splitlines()
        if not lines:
            return ToolResult(f"[{args.path} is empty]")

        start = args.offset or 1
        if start > len(lines):
            return ToolResult(
                f"offset {start} is past the end of {args.path} ({len(lines)} lines).",
                is_error=True)
        count = args.limit if args.limit is not None else DEFAULT_LINE_LIMIT
        window = lines[start - 1:start - 1 + count]
        end = start + len(window) - 1

        width = len(str(end))
        body = "\n".join(f"{n:>{width}}→ {text}"
                         for n, text in enumerate(window, start=start))

        # Self-bounding. Refusing beats truncating here: a refusal costs ~100
        # chars and the model retries with a narrower window, while a silent
        # truncation costs the full ceiling in tokens AND hides that there was
        # more. The exception is a single monstrous line — no smaller `limit`
        # can help, so that one gets cut with the cut declared.
        if len(body) > MAX_READ_CHARS:
            if len(window) == 1:
                return ToolResult(
                    body[:MAX_READ_CHARS]
                    + f"\n…[line {start} is {len(window[0])} chars; cut at "
                      f"{MAX_READ_CHARS}]")
            suggested = max(1, len(window) * MAX_READ_CHARS // len(body))
            return ToolResult(
                f"Lines {start}-{end} of {args.path} are {len(body)} chars, over the "
                f"{MAX_READ_CHARS}-char ceiling for one read. Retry with a smaller "
                f"window — about limit={suggested} — or grep for what you need.",
                is_error=True)

        remaining = len(lines) - end
        if remaining > 0:
            body += (f"\n\n…[{remaining} more line{'' if remaining == 1 else 's'}; "
                     f"read from offset {end + 1} to continue]")
        return ToolResult(body)


# ── write_file ───────────────────────────────────────────────────────────────
class WriteFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to write (created or overwritten).")
    content: str = Field(description="Full UTF-8 contents to write.")


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one with the given "
        "contents, inside your sandbox workspace. Use this for brand-new files or full "
        "rewrites; use edit_file for a targeted change to a file you have read. It is a "
        "mutating operation and runs one at a time. Writing to protected locations "
        "(.git internals, credentials, shell config) is stopped by the safety gate."
    )
    Args = WriteFileArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False

    async def call(self, args: WriteFileArgs, ctx: ToolContext) -> ToolResult:
        # Read first, purely so the operator gets a diff rather than a byte
        # count. An overwrite of an existing file is the case where seeing what
        # was lost matters most, and it is invisible after the write. A missing
        # file is a creation, which diffs against nothing.
        try:
            previous = await ctx.cell.read(args.path)
        except Exception:  # noqa: BLE001 — new file, unreadable, binary: all "no before"
            previous = ""

        await ctx.cell.write(args.path, args.content)
        ctx.files.record(args.path, args.content, _hash(args.content))
        return ToolResult(f"Wrote {len(args.content)} chars to {args.path}.",
                          display=render_edit(args.path, previous, args.content))


# ── edit_file ────────────────────────────────────────────────────────────────
class EditFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative path of the file to edit.")
    old_string: str = Field(description="Exact text to replace. Must appear exactly once.")
    new_string: str = Field(description="Replacement text.")


class EditFile(Tool):
    name = "edit_file"
    description = (
        "Make a targeted edit to a file by replacing an exact substring. You must have "
        "read the file first and it must not have changed since (read-before-write "
        "grounding), or the edit is rejected with an explanation. The old_string must "
        "match exactly once so the edit is unambiguous, and it must match the file's raw "
        "text — strip the line-number prefix that read_file adds for display. It is a "
        "mutating operation and runs one at a time."
    )
    Args = EditFileArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False

    async def call(self, args: EditFileArgs, ctx: ToolContext) -> ToolResult:
        try:
            current = await ctx.cell.read(args.path)
        except FileNotFoundError:
            return ToolResult(f"File {args.path!r} does not exist. Create it with write_file.",
                              is_error=True)

        err = ctx.files.freshness_error(args.path, _hash(current))
        if err:
            return ToolResult(err, is_error=True)

        occurrences = current.count(args.old_string)
        if occurrences == 0:
            return ToolResult(f"old_string not found in {args.path}. Read the file and retry.",
                              is_error=True)
        if occurrences > 1:
            return ToolResult(
                f"old_string appears {occurrences} times in {args.path}; it must be unique. "
                f"Include more surrounding context.", is_error=True)

        updated = current.replace(args.old_string, args.new_string)
        await ctx.cell.write(args.path, updated)
        ctx.files.record(args.path, updated, _hash(updated))
        # The model wrote this change and needs only "it applied"; the
        # operator needs to see what landed. `display` carries the diff to
        # them without it costing a place in the transcript.
        return ToolResult(f"Edited {args.path} (1 replacement).",
                          display=render_edit(args.path, current, updated))
