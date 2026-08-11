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
from forge.tui import ansi, keys
from forge.warden.ledger import TokenLedger
from forge.warden.oracle import UNANSWERED, Answer, Reply
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

    def __init__(self, spinner: Any = None, signal: Any = None) -> None:
        self.asked: list[tuple[str, str]] = []
        self.signal = signal
        """The turn's interrupt event, so a prompt nobody is answering can be
        abandoned.

        Without it, ctrl+c at a permission prompt did nothing at all. The read
        blocks in a worker thread, the turn is parked inside the tool dispatch
        waiting on it, and `repl._run_turn` answers the interrupt by setting
        this and then awaiting the loop — which cannot reach the boundary where
        it would be checked, because the tool it is running has not returned.
        The operator sees `Running run_command`, presses ctrl+c, and nothing
        happens. Watching the event here is what closes that gap: the prompt is
        the one place that can notice."""

        self.spinner = spinner
        """The live line, so it can be stopped while the question is on screen.

        Without this the prompt is unusable rather than merely ugly: the
        spinner repaints with a carriage return and a clear-to-end-of-line
        several times a second, so it erases the question, the `>` and every
        character being typed into it. Observed as "the permission prompt does
        not work" — the keystrokes were arriving, and nothing on screen said
        so."""

    def _abandoned(self) -> bool:
        """Has this turn been told to stop while the prompt was on screen?"""
        return self.signal is not None and self.signal.is_set()

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        self.asked.append((tool_name, action_key))
        if self._abandoned():
            # Already interrupted before the gate even asked. Answering
            # anything else would run the action the operator just stopped.
            return Answer(False, note="the run was interrupted before this was approved")
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
        choice = await _choose(self._abandoned)
        if choice is None:
            ansi.write()
            return Answer(False, note="interrupted at the prompt")
        if choice == ABANDONED:
            ansi.write()
            ansi.write(ansi.paint("  ⏹ interrupted — the action was not taken", "yellow"))
            return Answer(False, note="the operator interrupted the run while this "
                                      "was waiting to be approved")
        if choice == "once":
            return Answer(True)
        if choice == "always":
            return Answer(True, remember=True)
        if choice == "redirect":
            note = await asyncio.to_thread(_ask_redirect)
            if note:
                return Answer(False, note=note)
        return Answer(False, note="declined at the prompt")

    async def consult(self, question: str, options: list[str] | None = None) -> Reply:
        """An open question, to somebody who is right here.

        No timeout, for the same reason `ask` has none: the operator just typed,
        so they are demonstrably present. And unlike the permission prompt there
        is no safe default to start the cursor on — a question has no equivalent
        of "no", which is why an empty answer returns UNANSWERED and lets the
        agent proceed on its own judgement rather than being read as a refusal.
        """
        if self._abandoned():
            return UNANSWERED
        if self.spinner is not None:
            self.spinner.pause()
        try:
            ansi.write()
            ansi.write(ansi.paint("  ?  QUESTION", "bold", "cyan"))
            for line in question.splitlines() or [question]:
                ansi.write("    " + ansi.paint(line, "bold"))
            if options:
                ansi.write()
                for n, option in enumerate(options, 1):
                    ansi.write("    " + ansi.paint(f"{n}. {option}", "grey"))
            ansi.write()
            ansi.write(ansi.paint(
                "    answer, or press enter to let it decide", "dim"))
            text = (await asyncio.to_thread(_read_line, "    > ")).strip()
        finally:
            if self.spinner is not None:
                self.spinner.resume()

        if not text:
            return UNANSWERED
        # A bare number picks the option, so answering a three-way choice costs
        # one keystroke. Anything else is prose and passes through untouched.
        if options and text.isdigit() and 1 <= int(text) <= len(options):
            return Reply(answered=True, text=options[int(text) - 1])
        return Reply(answered=True, text=text)


def _read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        # Interrupting a question is not a refusal — there is nothing to refuse.
        # It means "stop asking me", which is what UNANSWERED tells the agent.
        return ""


_ABANDON_POLL_S = 0.15
"""How often a waiting prompt looks up to see whether its run still exists.

Fast enough that ctrl+c feels like it did something, slow enough that a prompt
left open all afternoon is not a busy-wait."""


_CHOICES = (
    ("once", "y", "Yes"),
    ("always", "a", "Yes, and don't ask again for this exact action"),
    ("no", "n", "No"),
    ("redirect", "t", "No, and tell it what to do instead"),
)
"""The answers, in Claude Code's order and shape, for two reasons it gets right.

**The scope lives in the label.** "always" told the operator nothing about what
they were granting. Forge records the EXACT action string and never a pattern
(see dispatch.py), so the label says exactly that — a standing permission is
the one answer nobody should agree to from a word they had to interpret.

**Refusing is not the end of the exchange.** A bare "no" tells the agent it may
not do this and nothing about what to do instead, so it guesses — and the most
likely guess is a way around the refusal. `Answer.note` already reaches the
model ("The operator declined this: <note>"), so the channel existed; nothing
was ever put into it.
"""


ABANDONED = "abandoned"
"""The turn was interrupted while this prompt was waiting to be answered.

Not one of `_CHOICES`: the operator did not decline the action, they stopped
the run that wanted it. Both end with the action not happening, and they read
completely differently in a transcript — "the operator said no to this" invites
the model to find another way, and "the run was stopped" does not."""


async def _choose(abandoned: Any = None) -> str | None:
    """The operator's answer, ABANDONED, or None if they interrupted.

    Arrows where the console can give us keys one at a time, a typed word where
    it cannot. Deliberately NOT built on prompt_toolkit: that is what powers
    the completion menu, and it is also what fails to construct in some
    terminals — which is the situation a permission prompt most needs to
    survive. Reading the console directly (forge/tui/keys.py) means the
    selector works in terminals where the line editor does not.

    `abandoned` is polled rather than awaited because the read it is racing
    happens in a thread, and a thread cannot be cancelled. Handing the
    predicate down to the reader — which already has to wake periodically to
    poll the console — costs nothing and is the only place with a loop to check
    it in.
    """
    if keys.available():
        picked = await asyncio.to_thread(_select_sync, abandoned)
        if picked is not None:
            return picked
    if abandoned is not None and abandoned():
        return ABANDONED
    return await asyncio.to_thread(_typed_sync)


def _render_options(cursor: int) -> None:
    for n, (name, key, blurb) in enumerate(_CHOICES):
        if n == cursor:
            ansi.write("  " + ansi.paint(f"❯ {name:<7} {blurb}", "bold", "cyan"))
        else:
            ansi.write("    " + ansi.paint(f"{name:<7} {blurb}", "grey"))


def _select_sync(abandoned: Any = None) -> str | None:
    """An arrow-driven list, repainted in place.

    Refusal is the cursor's starting position. On a gate, the answer reached by
    the least deliberate keystroke should be the one that does nothing.

    The read is bounded so the loop comes back a few times a second even when
    nobody touches the keyboard. That is not for the prompt's benefit — it is
    the only opportunity anything has to notice the turn was interrupted, and
    without it ctrl+c on a gated command did nothing whatsoever.
    """
    cursor = len(_CHOICES) - 1          # "no"
    _render_options(cursor)
    ansi.write(ansi.paint("    ↑↓ to choose · enter to confirm · or type y/a/n/t",
                          "dim"))

    poll = _ABANDON_POLL_S if abandoned is not None else None
    while True:
        key = keys.read_key(timeout=poll)
        if key is None:                 # console stopped cooperating mid-prompt
            return None
        if key == keys.NOTHING:
            # Nobody has answered yet. The only question worth asking between
            # keypresses is whether this prompt still has a run behind it.
            if abandoned is not None and abandoned():
                return ABANDONED
            continue
        if key == keys.CANCEL:
            return "no"
        if key == keys.ENTER:
            return _CHOICES[cursor][0]
        if key in ("y", "a", "n", "t"):
            return {"y": "once", "a": "always",
                    "n": "no", "t": "redirect"}[key]
        if key in (keys.UP, keys.DOWN):
            cursor = (cursor + (-1 if key == keys.UP else 1)) % len(_CHOICES)
            # Reclaim the rows just drawn and paint the new selection over
            # them, so the list updates rather than accumulating copies.
            if not ansi.rewind(len(_CHOICES) + 1):
                return None             # cannot repaint: fall back to typing
            _render_options(cursor)
            ansi.write(ansi.paint(
                "    ↑↓ to choose · enter to confirm · or type y/a/n/t", "dim"))


def _ask_redirect() -> str:
    """What the operator wants done instead.

    Reaches the model as the denial's reason, so it is an instruction and not a
    complaint — "read the .env.example instead" turns a dead end into the next
    step. Empty falls back to a plain refusal rather than sending the model an
    empty string to interpret.
    """
    ansi.write(ansi.paint("    what should it do instead?", "grey"))
    try:
        said = input("    > ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return f"the operator refused and said: {said}" if said else ""


def _typed_sync() -> str | None:
    for name, key, blurb in _CHOICES:
        ansi.write(ansi.paint(f"    {key}  {blurb}", "grey"))
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
    if raw in ("t", "tell", "redirect", "4"):
        return "redirect"
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
    context_warned: bool = False
    """Whether this session has already been told its context is filling up.
    Lives here rather than in a module-level registry because the alternative —
    keying on `id(session)` — silently misfires when CPython reuses an address.
    Cleared by `/clear` and `/compact`; see `status.forget_pressure`."""
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    """Conversation checkpoints taken with /checkpoint. Each is {turn, messages,
    timestamp}. /restore reverts to one of these, discarding later turns."""
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
