"""Nudges the loop injects into itself, when the state earns one.

The system prompt is read once and then competes with everything that has
happened since. A reminder arrives at the moment it applies, attached to the
tool results that made it true, which is why it lands when the prompt did not.

The mechanism is trivial — a `<system-reminder>` block appended to the user
message carrying the tool results. Everything worth arguing about is the
CONDITIONS. Each rule here corresponds to a failure that actually happened in
this repository, not to a principle someone thought sounded good:

- an agent called the same tool with the same arguments three times running,
  because the error told it to pass an argument the tool did not expose
- an agent wrote a tool, ran it, got a number that was wrong by an order of
  magnitude, and wrote a paragraph explaining the number instead of checking it
- an agent edited files across a long job and never ran anything
- an agent ran a formatter, then edited a file against the version it had read
  before the formatter touched it (see `file_changed_notice`, which is a
  statement of fact rather than a rule, and so is exempt from the first
  constraint below)

Three constraints keep them from becoming wallpaper, which is the only way a
reminder system fails:

**Once each, per job.** A rule that fires repeatedly teaches the reader to skip
the block, and then the one that mattered is skipped too.

**Never when the thing is already happening.** A "you have not run the tests"
that fires while the tests are running is worse than silence, because it says
the harness is not watching.

**Specific enough to act on.** "Remember to verify" is furniture. "You have
called grep with these exact arguments twice and both failed" is a next step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from forge.warden.state import LoopState

# Below this the loop is too young for most judgements: an agent three
# iterations in that has not written a plan is not disorganised, it is reading.
_SETTLING_IN = 6

WRITE_TOOLS = frozenset({"write_file", "edit_file"})
CHECK_TOOLS = frozenset({"run_command", "diagnostics"})


@dataclass
class ReminderState:
    """What the rules read. Distinct from LoopState because these are
    observations ABOUT the run rather than the run itself, and because a rule
    that could reach into the loop would eventually change it."""

    iteration: int = 0
    wrote_at: int = 0
    checked_at: int = 0
    planned: bool = False
    repeated_failure: tuple[str, int] | None = None
    """(tool name, how many times) for a call repeated with identical
    arguments and failing every time."""

    fired: set[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.fired is None:
            self.fired = set()


@dataclass(frozen=True)
class Reminder:
    name: str
    when: Callable[[ReminderState], bool]
    text: Callable[[ReminderState], str]


def _repeating(s: ReminderState) -> bool:
    return s.repeated_failure is not None and s.repeated_failure[1] >= 2


def _repeating_text(s: ReminderState) -> str:
    tool, n = s.repeated_failure          # type: ignore[misc]
    return (
        f"You have now called `{tool}` {n} times with the same arguments and it "
        "has failed every time. It will fail again.\n\n"
        "Read the error text itself rather than the fact that it failed — it "
        "often names the way through, and the way through is sometimes an "
        "argument this tool does not accept, in which case no retry will ever "
        "work. Change the arguments, use a different tool, or say plainly that "
        "you are blocked and why."
    )


def _unverified(s: ReminderState) -> bool:
    return (s.wrote_at > 0
            and s.checked_at < s.wrote_at
            and s.iteration - s.wrote_at >= 3)


def _unverified_text(s: ReminderState) -> str:
    return (
        "You have changed files and not run anything since. Nothing you have "
        "written has been shown to work.\n\n"
        "Run the thing — the tests, the program, a type check, `diagnostics`. "
        "Doing it now costs one call; doing it at the end means unpicking "
        "which of several changes broke what."
    )


def _unplanned(s: ReminderState) -> bool:
    return not s.planned and s.iteration >= 12


def _unplanned_text(s: ReminderState) -> str:
    return (
        f"You are {s.iteration} tool calls into this job with no plan recorded. "
        "If it has more than a couple of steps, write one with `todo_write`.\n\n"
        "It is not bookkeeping: the plan survives context compaction, so it is "
        "what stops a long job losing its own thread when earlier turns are "
        "summarised away."
    )


REMINDERS: tuple[Reminder, ...] = (
    # Ordered by how expensive the failure is to discover later.
    Reminder("repeated_failure", _repeating, _repeating_text),
    Reminder("unverified_changes", _unverified, _unverified_text),
    Reminder("no_plan", _unplanned, _unplanned_text),
)


def file_changed_notice(paths: list[str]) -> str:
    """Files the model has read that something else has since rewritten.

    Not a rule in REMINDERS, and not subject to the once-per-job restraint the
    rules live under, because it is a different kind of statement. A rule is a
    judgement about how the run is going, and repeating a judgement is nagging.
    This is a fact about the workspace that the model cannot observe and that
    stops being true the moment it re-reads. Suppressing the second occurrence
    would mean the second formatter run goes unannounced — which is precisely
    when an edit lands on text nobody has looked at.

    A linter, a formatter, a build step, the operator in their editor, or a
    subagent working in the same tree: all of them move a file out from under a
    read the model still believes in. Without this the model edits against what
    it saw ten minutes ago, and the edit either fails on a stale match or
    silently reverts somebody else's change — the second being much the worse,
    because it looks like it worked.
    """
    listed = "\n".join(f"  - {p}" for p in paths)
    changed = "This file has" if len(paths) == 1 else "These files have"
    return (
        "<system-reminder>\n"
        f"{changed} changed on disk since you read {'it' if len(paths) == 1 else 'them'}, "
        "and not because of anything you did:\n\n"
        f"{listed}\n\n"
        "Something else wrote there — a formatter or linter you ran, a build "
        "step, a subagent, or the operator. What you remember of "
        f"{'this file' if len(paths) == 1 else 'these files'} is now out of "
        "date. Read again before editing, and before drawing any conclusion "
        "from the contents.\n"
        "</system-reminder>"
    )


def due(state: ReminderState) -> str | None:
    """The one reminder this iteration has earned, or None.

    One at a time, deliberately. Two nudges in a single turn read as the
    harness panicking, and the second is skipped along with the first.
    """
    if state.iteration < _SETTLING_IN:
        return None
    for rule in REMINDERS:
        if rule.name in state.fired:
            continue
        if rule.when(state):
            state.fired.add(rule.name)
            return f"<system-reminder>\n{rule.text(state)}\n</system-reminder>"
    return None


def observe(state: ReminderState, loop: LoopState, tool_uses, results) -> None:
    """Fold one iteration's tool calls into the observations.

    A repeat only counts when the arguments are identical AND the result was an
    error: a tool called twice the same way that worked twice is a loop doing
    its job, and one called twice differently is an agent adapting.
    """
    state.iteration = loop.iteration
    for tu, res in zip(tool_uses, results):
        if tu.name in WRITE_TOOLS and not res.is_error:
            state.wrote_at = loop.iteration
        elif tu.name in CHECK_TOOLS:
            state.checked_at = loop.iteration
        elif tu.name == "todo_write":
            state.planned = True

        if res.is_error:
            key = (tu.name, repr(sorted((tu.input or {}).items())))
            prev_key, count = getattr(state, "_last_error", (None, 0))
            count = count + 1 if key == prev_key else 1
            state._last_error = (key, count)      # type: ignore[attr-defined]
            state.repeated_failure = (tu.name, count)
        else:
            # A success clears it: whatever it was doing, it is no longer stuck
            # on that call, and reminding it about a resolved problem is noise.
            state._last_error = (None, 0)         # type: ignore[attr-defined]
            state.repeated_failure = None
