"""One interactive session: state that outlives a turn, and how a turn renders.

The Warden runs one job and returns a Terminal. A conversation is many of those
sharing a transcript, a ledger, a Cell and a set of standing approvals — so the
session owns those and hands them to each turn, rather than a turn owning
anything.

The terminal oracle is the piece worth pointing at. It is the third
implementation of Seam 2 (after auto-deny and the peer socket), it took eleven
lines, and no core module knew it was coming. That is the whole claim the seam
was built to make.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.agents.config import AgentConfig
from forge.tui import ansi
from forge.warden.ledger import TokenLedger
from forge.warden.oracle import Answer
from forge.warden.permissions import AllowList
from forge.warden.tool import Tool


class TerminalOracle:
    """Ask the operator, who is right here.

    No timeout: the peer's oracle races a countdown because nobody may be
    watching, but here somebody demonstrably is — they just typed. A prompt that
    expired while they were reading the command would be hostile, and the answer
    to "are you still there" is that the cursor is blinking at them.

    Ctrl-C at the prompt is a refusal, not a crash: interrupting the question is
    a perfectly clear way to say no."""

    def __init__(self, spinner: Any = None) -> None:
        self.asked: list[tuple[str, str]] = []
        self.spinner = spinner
        """The live line, so it can be stopped while the question is on screen.

        Without this the prompt is unusable rather than merely ugly: the
        spinner repaints with a carriage return and a clear-to-end-of-line
        several times a second, so it erases the question, the `>` and every
        character being typed into it. Observed as "the permission prompt does
        not work" — the keystrokes were arriving, and nothing on screen said
        so."""

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        self.asked.append((tool_name, action_key))
        if self.spinner is not None:
            self.spinner.pause()
        try:
            return await self._ask(tool_name, action_key, reason)
        finally:
            if self.spinner is not None:
                self.spinner.resume()

    async def _ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        ansi.write()
        ansi.write(ansi.paint("  ⚠  PERMISSION", "bold", "orange") + ansi.paint(
            f"  {tool_name}", "orange"))
        ansi.write(ansi.paint(f"  {reason}", "grey"))
        ansi.write()
        # Verbatim and unwrapped: approving a force-push means seeing the branch.
        for line in action_key.splitlines() or [action_key]:
            ansi.write("    " + ansi.paint(line, "bold"))
        ansi.write()
        choice = await _choose()
        if choice is None:
            ansi.write()
            return Answer(False, note="interrupted at the prompt")
        if choice == "once":
            return Answer(True)
        if choice == "always":
            return Answer(True, remember=True)
        return Answer(False, note="declined at the prompt")


# The three answers, in the order they should be offered: the safe one first,
# so a reflexive Enter approves this call only and never grants a standing
# permission the operator did not read.
_CHOICES = (
    ("once", "y", "allow this one call"),
    ("always", "a", "allow this exact action from now on"),
    ("no", "n", "refuse it"),
)


async def _choose() -> str | None:
    """The operator's answer, or None if they interrupted.

    Typed, not a cursor menu. A selectable list would be better — `a` grants a
    STANDING permission and is reachable by the least deliberate keystroke on
    the row — but a menu needs the line editor, and the line editor is exactly
    what does not build in every terminal this runs in. A half-drawn selector
    that silently falls back to typing is worse than an honest prompt: it looks
    like arrows work, and they do not.

    So the typed prompt is made good instead. Each option carries its
    consequence on the row, so it is read while choosing rather than recalled
    from a legend, and input is accepted generously.
    """
    return await asyncio.to_thread(_typed_sync)


def _typed_sync() -> str | None:
    for name, key, blurb in _CHOICES:
        ansi.write(ansi.paint(f"    {key}  {name:<7} {blurb}", "grey"))
    try:
        raw = input("    > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    # A bare Enter REFUSES. It is the likeliest accident at a prompt someone is
    # still reading, and the safe landing for an accident on a permission gate
    # is "nothing happened" — not "one call happened". Approval costs a
    # deliberate keystroke, which is the entire point of asking.
    if raw in ("y", "yes", "once", "1"):
        return "once"
    if raw in ("a", "always", "2"):
        return "always"
    return "no"


@dataclass
class Session:
    """Everything a conversation carries between turns."""

    cfg: AgentConfig
    model_ref: str
    workspace: Path
    tools: dict[str, Tool]
    ledger: TokenLedger
    allowlist: AllowList
    cell: Any = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    oracle: TerminalOracle = field(default_factory=TerminalOracle)
    turns: int = 0
    _mode: str = ""
    last_truncated: tuple[str, str] | None = None
    input_bar: Any = None
    session_id: str = ""
    """Filename this conversation persists to. A resumed session keeps its
    own id, so continuing it updates the same record rather than forking a
    second one that diverges from the first."""
    """The live input line, so a command can change how it reads keys."""
    """(tool, full result) for the most recent shortened output — what
    ctrl+o puts back."""

    @property
    def permission_mode(self) -> str:
        """The live mode. Starts at the profile's and can be cycled with
        shift+tab; AgentConfig is frozen because it is loaded config, and a
        session-lifetime toggle is not config."""
        return self._mode or self.cfg.permission_mode

    def set_permission_mode(self, mode: str) -> None:
        self._mode = mode

    def reset(self) -> None:
        """Forget the conversation, keep the session.

        The ledger's running costs survive deliberately: `/clear` frees context,
        it does not un-spend money, and a cost display that reset would quietly
        under-report what the session actually cost."""
        self.messages = []
        self.ledger.prompt_tokens = 0
