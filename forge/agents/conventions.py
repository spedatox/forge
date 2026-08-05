"""The repository's own instructions to an agent working in it.

A coding agent that has not read a project's conventions rediscovers them by
being corrected: it picks the wrong test runner, writes a docstring style
nobody else uses, adds a dependency the project deliberately avoids. Each of
those costs a turn to find and another to fix, every session, forever.

`PromptFragment` has anticipated this from the start — its `source` field
documents `"repo:CLAUDE.md"` as a value — but nothing ever produced one. This
is what does.

**Filenames, in order.** `AGENTS.md` first: it is the name several tools have
converged on and the one that does not imply a specific vendor. `CLAUDE.md`
next, because a great many repositories already have one and asking an operator
to duplicate it would be asking them to maintain two copies of the same file.

Read from the workspace root only. Claude Code walks subdirectories and merges
what it finds; that is a real feature and also a way to assemble a system
prompt nobody can predict, so this stays at one file until there is a concrete
reason for more.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAMES = ("AGENTS.md", "CLAUDE.md", ".forge/AGENTS.md")

# Past this the file has stopped being conventions and become documentation,
# and it is being paid for on every single turn.
MAX_CHARS = 12_000


def find(workspace: Path) -> Path | None:
    """The conventions file for this workspace, or None."""
    for name in FILENAMES:
        candidate = workspace / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load(workspace: Path) -> tuple[str, str] | None:
    """(source label, text), or None when there is nothing to load.

    Never raises: an unreadable conventions file should cost its own contents,
    not the session.
    """
    path = find(workspace)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        logger.warning("conventions_unreadable", extra={"path": str(path), "error": repr(e)})
        return None
    if not text:
        return None
    if len(text) > MAX_CHARS:
        text = (text[:MAX_CHARS]
                + f"\n\n[truncated at {MAX_CHARS} characters — this file is being "
                  "sent on every turn; consider shortening it]")
    return f"repo:{path.name}", text


def fragment(workspace: Path):
    """A PromptFragment for the composer, or None.

    Labelled with its filename rather than folded into the profile, because an
    agent handed both its own instructions and a repository's needs to know
    which is which to resolve a conflict between them.
    """
    from forge.agents.prompt import PromptFragment

    loaded = load(workspace)
    if loaded is None:
        return None
    source, text = loaded
    return PromptFragment(source, text)
