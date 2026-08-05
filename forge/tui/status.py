"""The status line — model, context, spend, mode, branch, directory.

Claude Code keeps this pinned to the bottom of the screen because Ink owns a
dynamic region down there. An inline renderer has no such region: anything
pinned to the last row has to be redrawn on every scroll, which fights the
terminal's own scrollback instead of using it.

So this is drawn **above the prompt**, once per input. It scrolls away with
everything else, and it appears at exactly the moment its numbers are
actionable — when the operator is deciding what to type next. Pinning it would
show the same information continuously and be read at that same moment anyway.

    optimus · deepseek-v4-pro · 18% ctx · 12.4k in / 3.1k out · act · main · forgetest

Cost in real money is deliberately absent unless the operator supplies prices.
Token counts are measured; a dollar figure derived from a rate table baked into
this file would be a guess wearing a measurement's clothes, and it would go
stale silently the first time a provider changed a price. PRICES below is empty
on purpose — fill it in and the line starts showing money.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from forge.tui import ansi

if TYPE_CHECKING:
    from forge.tui.session import Session

# model ref → (USD per million input tokens, USD per million output tokens).
# Operator-maintained and empty by default: see the module docstring. Cached
# reads are billed differently again, so a rate here is already an approximation
# and the line says "~" to admit it.
PRICES: dict[str, tuple[float, float]] = {}

_BRANCH_CACHE: dict[str, str] = {}


def _humanize(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _short_model(ref: str) -> str:
    """`deepseek:deepseek-v4-pro` → `deepseek-v4-pro`. The provider is already
    implied by the key that had to be set for it to run at all."""
    return ref.split(":", 1)[1] if ":" in ref else ref


def git_branch(workspace: Path) -> str:
    """The current branch, or "" when this is not a repository.

    Cached per directory: this runs before every prompt and a subprocess per
    keystroke-pause is a real cost on Windows. A branch change mid-session is
    rare enough to be worth the staleness, and `/status` re-reads it.
    """
    key = str(workspace)
    if key in _BRANCH_CACHE:
        return _BRANCH_CACHE[key]
    branch = ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            branch = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    _BRANCH_CACHE[key] = branch
    return branch


def forget_branch(workspace: Path) -> None:
    """Drop the cached branch — after a worktree switch, or on /status."""
    _BRANCH_CACHE.pop(str(workspace), None)


def estimate_cost(model_ref: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD, or None when no price is configured for this model."""
    rate = PRICES.get(model_ref) or PRICES.get(_short_model(model_ref))
    if rate is None:
        return None
    per_in, per_out = rate
    return (input_tokens / 1_000_000) * per_in + (output_tokens / 1_000_000) * per_out


def segments(session: "Session") -> list[str]:
    """The parts, in priority order — first dropped last when space is short."""
    led = session.ledger
    parts = [session.cfg.agent_id, _short_model(session.model_ref)]

    if led.turns:
        parts.append(f"{int(100 * led.fullness)}% ctx")
        parts.append(f"{_humanize(led.input_tokens)} in / {_humanize(led.output_tokens)} out")
        cost = estimate_cost(session.model_ref, led.input_tokens, led.output_tokens)
        if cost is not None:
            parts.append(f"~${cost:.2f}")

    mode = session.permission_mode
    if mode != "act":
        # Only when it is NOT the default: a line that always says "act" trains
        # the eye to skip it, and "plan" is exactly the thing worth noticing.
        parts.append(mode)

    if branch := git_branch(session.workspace):
        parts.append(branch)

    parts.append(session.workspace.name or str(session.workspace))
    return parts


def render(session: "Session") -> str:
    """One dim line, trimmed to the terminal. Empty when there is nothing to say."""
    parts = segments(session)
    if not parts:
        return ""
    width = ansi.terminal_width()
    line = "  " + " · ".join(parts)
    while len(line) > width - 1 and len(parts) > 2:
        # Drop from the middle: the agent and the directory are the two that
        # orient you, and they are the ends.
        parts.pop(len(parts) // 2)
        line = "  " + " · ".join(parts)
    return ansi.paint(ansi.truncate(line, max(10, width - 1)), "dim")


def write(session: "Session") -> None:
    if os.environ.get("FORGE_NO_STATUS"):
        return
    line = render(session)
    if line:
        ansi.write(line)
