"""Turning an edit into a diff — the text of it, with no terminal in sight.

`Edited calc.py (1 replacement)` tells the operator that something happened and
nothing about what. Reviewing the agent's work then means opening the file and
reconstructing the change by eye — which is the moment most people stop reading
and start trusting, and trusting an agent's self-report is the failure this
whole system keeps running into.

A diff is the smallest thing that makes review actually happen.

Built on difflib, so no dependency, and deliberately unified-with-context rather
than whole-file: an edit to line 400 of a 900-line file should print six lines,
not nine hundred. Everything here is pure text in, pure text out, so it is
testable without a terminal and reusable by any surface that gets the
tool_result event. Colour is added at the edge (forge/tui/render.py), never
here: the same diff has to be able to go to a log, a socket, or a terminal
with no colour at all.
"""
from __future__ import annotations

import difflib

CONTEXT_LINES = 3
# Past this an edit is not reviewable in a scrollback anyway; the operator wants
# `git diff`, and saying so beats printing four hundred lines they will skip.
MAX_DIFF_LINES = 60


def _hunk_header(old_start: int, old_len: int, new_start: int, new_len: int) -> str:
    return f"@@ -{old_start},{old_len} +{new_start},{new_len} @@"


def unified(old: str, new: str, path: str = "",
            context: int = CONTEXT_LINES, max_lines: int = MAX_DIFF_LINES) -> str:
    """A plain unified diff, uncoloured. Empty string when nothing changed."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if old_lines == new_lines:
        return ""

    out: list[str] = []
    for group in difflib.SequenceMatcher(None, old_lines, new_lines).get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        out.append(_hunk_header(first[1] + 1, last[2] - first[1],
                                first[3] + 1, last[4] - first[3]))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend(f" {line}" for line in old_lines[i1:i2])
                continue
            if tag in ("replace", "delete"):
                out.extend(f"-{line}" for line in old_lines[i1:i2])
            if tag in ("replace", "insert"):
                out.extend(f"+{line}" for line in new_lines[j1:j2])

    if len(out) > max_lines:
        kept = out[:max_lines]
        kept.append(f"… {len(out) - max_lines} more diff lines — run `git diff` to see it all")
        out = kept
    return "\n".join(out)



def summarize(old: str, new: str) -> str:
    """`+3 -1` — the one-line shape of a change, for a tool row."""
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    parts = []
    if added:
        parts.append(f"+{added}")
    if removed:
        parts.append(f"-{removed}")
    return " ".join(parts) or "no change"


def render_edit(path: str, old: str, new: str) -> str:
    """What an edit's `display` carries: a summary line and the diff."""
    body = unified(old, new, path)
    if not body:
        return f"{path}: no change"
    return f"{path}  {summarize(old, new)}\n{body}"
