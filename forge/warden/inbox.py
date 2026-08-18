"""Operator input that arrives while the loop is already running.

Until now the prompt existed only *between* turns: you asked, you watched, and
if the agent set off in the wrong direction at iteration two you had exactly two
options — sit through forty more iterations, or ctrl+c and lose the turn. Both
references fixed this and neither treats it as a nicety. DSH ships `steer()`,
`followup()` and `inject()` as three presets over one `send()` primitive; Codex
keeps its composer live under a running task.

The mechanism is a queue the loop drains at its own boundaries. That is the
whole of it, and the restraint is deliberate: input is *claimed by the loop*
rather than pushed into it, so a message can never land halfway through a tool
batch or between an assistant turn and its tool results, which are the two
places a transcript can actually be corrupted.

**Where it lands.** At the tool-result boundary the claimed text rides in the
same user message as the results, as an extra text block — exactly the route
`reminders` already uses for its nudges. That keeps the strict user/assistant
alternation that compaction's `find_cut` and `rebuild` depend on. A separate
message would be simpler to write and would quietly break both.

**When the turn is ending.** If the model stops asking for tools while input is
still pending, the turn does NOT end: the pending text becomes a user message
and the loop continues. This is DSH's "claims pending next-step input plus one
queued prompt at a turn boundary", and it is the case that matters most — you
type "also update the README" while it is writing its summary, and it picks it
up instead of making you start a turn to say it.

**Nothing is claimed twice**, and nothing is lost on abort: an interrupted turn
leaves whatever was pending still pending, and the REPL hands it back as the
next prompt rather than discarding what the operator took the trouble to type.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Inbox:
    """A single queue of operator messages awaiting the next loop boundary.

    One queue rather than DSH's two (`next-step` and `next-turn`), because the
    distinction there is *when to wake the driver* and Forge's loop is never
    asleep — it is always either streaming or running tools, and both end at a
    boundary within seconds. Splitting the queue would create a difference the
    operator can neither observe nor control, and a second thing for the TUI to
    explain.
    """

    _pending: list[str] = field(default_factory=list)

    def push(self, text: str) -> None:
        """Queue one message. Blank input is dropped rather than queued — an
        empty line is how a person clears the composer, not something to say."""
        text = (text or "").strip()
        if text:
            self._pending.append(text)

    def claim(self) -> list[str]:
        """Take everything pending. The queue is emptied atomically from the
        loop's point of view, because the loop is the only claimer and it runs
        on one task."""
        if not self._pending:
            return []
        taken, self._pending = self._pending, []
        return taken

    def peek(self) -> list[str]:
        """What is waiting, without taking it. For the TUI's own display and
        for handing unclaimed input back after an abort."""
        return list(self._pending)

    def __bool__(self) -> bool:
        return bool(self._pending)

    def __len__(self) -> int:
        return len(self._pending)


def render(messages: list[str]) -> str:
    """How claimed input is presented to the model.

    Marked as arriving mid-run, because otherwise it reads as part of the
    original task and the model tries to reconcile it with instructions it was
    given before anything had happened. Saying WHEN it arrived is what makes
    "actually, use the other file" land as a correction rather than as a
    contradiction the model has to resolve.

    Framed as outranking the earlier instruction on purpose: a person who
    interrupts a running job to say something is not offering a suggestion, and
    a model that weighs it equally against the original prompt will average the
    two and satisfy neither.
    """
    body = "\n\n".join(m.strip() for m in messages if m.strip())
    plural = "message" if len(messages) == 1 else "messages"
    return (
        f"<operator_interjection>\n"
        f"The operator sent {'this' if len(messages) == 1 else 'these'} {plural} "
        f"while you were working, after the task you were given:\n\n"
        f"{body}\n\n"
        f"This is newer than your original instructions and takes precedence "
        f"where they conflict. Adjust what you are doing now — do not finish the "
        f"previous approach first, and do not ask whether to apply it.\n"
        f"</operator_interjection>"
    )
