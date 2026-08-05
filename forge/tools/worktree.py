"""`enter_worktree` / `exit_worktree` — work without touching the checkout.

`forge chat` runs in the directory the operator is standing in. That is the
point of it, and it is also the reason an agent loose in a real repository is
alarming rather than useful: every edit lands in the working tree the operator
has open in their editor, mixed in with whatever they were doing themselves.

A git worktree is the tool git already provides for this. `git worktree add`
gives a second checkout of the same repository, on its own branch, sharing the
object store — cheap, native, and reviewable with the ordinary diff and merge
commands the operator already knows. The agent works there; the operator's
checkout does not move.

Two things make this isolation rather than decoration:

**The boundary moves with it.** `Cell.enter_subpath` narrows the escape check
as well as the working directory, so inside a worktree a write to
`../the-real-checkout/x` is refused by the Cell, not merely discouraged in a
prompt.

**Leaving is explicit.** `exit_worktree` puts the agent back at the workspace
root and says what is still uncommitted, because a worktree silently abandoned
with work in it is indistinguishable from work that was never done.

Not a sandbox. A worktree shares the repository — `git push` from inside one
still pushes, and `run_command` still runs. It bounds *where edits land*, which
is the failure that actually happens, not what a hostile command could do.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

# Worktrees live together under one directory so they are easy to see, easy to
# .gitignore, and obviously not part of the source tree.
WORKTREE_DIR = ".forge-worktrees"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")


def _no_cell(ctx: ToolContext) -> ToolResult | None:
    if ctx.cell is None:
        return ToolResult("No Cell is attached; worktrees are unavailable.", is_error=True)
    return None


async def _git(ctx: ToolContext, command: str, timeout: int = 60):
    return await ctx.cell.run(command, timeout=timeout)


class EnterWorktreeArgs(BaseModel):
    name: str = Field(
        description="Short name for the worktree and its branch, e.g. 'fix-retry'. "
                    "Letters, digits, dot, dash, underscore.")
    branch: str | None = Field(
        default=None,
        description="Branch to create. Defaults to forge/<name>. An existing "
                    "branch is checked out instead of created.")


class EnterWorktree(Tool):
    name = "enter_worktree"
    description = (
        "Create a git worktree and move your working directory into it, so your "
        "edits land on a separate branch instead of the operator's checkout. Use "
        "it BEFORE starting any multi-file change in a repository the operator "
        "actively works in — they can then review your branch with a normal diff "
        "and merge it, or throw it away, without your work ever having touched "
        "their files. Do NOT use it for read-only investigation, in a directory "
        "that is not a git repository, or when the operator asked you to edit "
        "their current checkout directly. While a worktree is active the Cell "
        "refuses writes outside it. Returns the worktree path and branch, or an "
        "is_error explaining why one could not be made."
    )
    Args = EnterWorktreeArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False   # additive: it creates a branch, it removes nothing

    async def call(self, args: EnterWorktreeArgs, ctx: ToolContext) -> ToolResult:
        if (bad := _no_cell(ctx)) is not None:
            return bad
        if ctx.cell.subpath:
            return ToolResult(
                f"Already working in a worktree ({ctx.cell.subpath}). Call "
                "exit_worktree before entering another.", is_error=True)
        if not _SAFE_NAME.match(args.name):
            return ToolResult(
                f"Refused: {args.name!r} is not a usable worktree name. Use "
                "letters, digits, dot, dash or underscore.", is_error=True)

        probe = await _git(ctx, "git rev-parse --is-inside-work-tree")
        if probe.exit_code != 0:
            return ToolResult(
                "This workspace is not a git repository, so there is no worktree "
                "to make. Either work here directly or ask the operator to init a "
                "repo first.", is_error=True)

        branch = args.branch or f"forge/{args.name}"
        rel = f"{WORKTREE_DIR}/{args.name}"

        exists = await _git(ctx, f"git rev-parse --verify --quiet refs/heads/{branch}")
        if exists.exit_code == 0:
            add = f"git worktree add {rel} {branch}"        # check the branch out
        else:
            add = f"git worktree add -b {branch} {rel}"     # create it
        made = await _git(ctx, add, timeout=120)
        if made.exit_code != 0:
            detail = (made.stderr or made.stdout or "").strip()[:300]
            return ToolResult(f"Could not create the worktree: {detail}", is_error=True)

        ctx.cell.enter_subpath(rel)
        if ctx.files is not None:
            # Same-named files in the worktree are different files; read-before-
            # write grounding from the main checkout does not carry over.
            ctx.files.clear()
        return ToolResult(
            f"Working in worktree {rel} on branch {branch}.\n"
            "Edits now land here, not in the operator's checkout, and writes "
            "outside this directory are refused. Tell them the branch name when "
            "you are done so they can review it."
        )


class ExitWorktreeArgs(BaseModel):
    pass


class ExitWorktree(Tool):
    name = "exit_worktree"
    description = (
        "Leave the active worktree and return to the workspace root. Use it when "
        "the change is finished and committed, or when you need to look at the "
        "operator's actual checkout again. It does NOT delete the worktree or the "
        "branch — the work stays for the operator to review — and it does not "
        "commit anything for you. Returns what is still uncommitted in the "
        "worktree, so nothing is abandoned silently."
    )
    Args = ExitWorktreeArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False

    async def call(self, args: ExitWorktreeArgs, ctx: ToolContext) -> ToolResult:
        if (bad := _no_cell(ctx)) is not None:
            return bad
        rel = ctx.cell.subpath
        if not rel:
            return ToolResult("Not in a worktree — already at the workspace root.")

        status = await _git(ctx, "git status --porcelain")
        dirty = [ln for ln in (status.stdout or "").splitlines() if ln.strip()]
        branch = await _git(ctx, "git rev-parse --abbrev-ref HEAD")
        name = (branch.stdout or "").strip() or "(unknown)"

        ctx.cell.leave_subpath()
        if ctx.files is not None:
            ctx.files.clear()

        if dirty:
            listed = "\n".join(f"  {ln}" for ln in dirty[:10])
            more = f"\n  … and {len(dirty) - 10} more" if len(dirty) > 10 else ""
            return ToolResult(
                f"Left worktree {rel} (branch {name}). It has UNCOMMITTED changes:\n"
                f"{listed}{more}\n\nSay so plainly — uncommitted work in an "
                "abandoned worktree reads to the operator as work never done."
            )
        return ToolResult(
            f"Left worktree {rel} (branch {name}); working tree clean. The branch "
            "is there for the operator to review or merge."
        )
