"""The run's plan (§3 companion: the iteration ceiling bounds a run, this is
what keeps a long one coherent).

A model working a twenty-step job holds the plan in the transcript and nowhere
else, so the plan degrades exactly as the transcript does: elision removes tool
results, compaction replaces whole stretches with a summary, and what is lost
first is the boring bookkeeping — which of the six things are done. The failure
is not dramatic. The agent finishes step four, forgets steps five and six, and
reports success.

So the plan lives here instead: harness-side, outside the transcript, immune to
reclamation. `engine._compact` re-states it into the summary, which is the one
message compaction is guaranteed to keep.

Deliberately NOT persisted to disk. A todo list that outlives its run would
greet the next one with a stranger's half-finished plan, and the workspace is
the operator's repo — writing a scratch file into it is a side effect nobody
asked for.
"""
from __future__ import annotations

from dataclasses import dataclass

PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
STATUSES = (PENDING, IN_PROGRESS, COMPLETED)

_MARKER = {PENDING: " ", IN_PROGRESS: "~", COMPLETED: "x"}

MAX_ITEMS = 40
"""Past this it is not a plan, it is a transcript with checkboxes."""


@dataclass(frozen=True)
class Todo:
    content: str
    status: str

    def render(self) -> str:
        return f"[{_MARKER.get(self.status, '?')}] {self.content}"


class TodoList:
    """The current plan. Replaced wholesale, never patched.

    Whole-list replacement rather than per-item mutation is the same trade
    `write_file` makes against a patch API: the model sends what the list should
    now be, so there is no item-id bookkeeping to get wrong and no way to
    silently desync from what the operator is reading.
    """

    def __init__(self) -> None:
        self._items: list[Todo] = []

    def __bool__(self) -> bool:
        return bool(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[Todo]:
        return list(self._items)

    def replace(self, items: list[Todo]) -> None:
        self._items = list(items)

    def clear(self) -> None:
        self._items = []

    def counts(self) -> tuple[int, int, int]:
        """(done, in progress, pending)"""
        done = sum(1 for t in self._items if t.status == COMPLETED)
        active = sum(1 for t in self._items if t.status == IN_PROGRESS)
        return done, active, len(self._items) - done - active

    def render(self, header: str = "PLAN") -> str:
        if not self._items:
            return f"{header}: (empty)"
        done, _, _ = self.counts()
        lines = [f"{header} ({done}/{len(self._items)} done):"]
        lines.extend(f"  {t.render()}" for t in self._items)
        return "\n".join(lines)

    def unfinished(self) -> list[Todo]:
        return [t for t in self._items if t.status != COMPLETED]
