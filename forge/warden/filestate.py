"""FileStateCache — read-before-write grounding (study §3).

The agent holds no live model of the Cell filesystem. It tracks only files it has
touched: content + the mtime it saw. Edits are gated on 'you read this, and it
hasn't changed since' — the single highest-value pattern for keeping edits
grounded in reality without a filesystem watcher. Kept small (an LRU) so a long
session can't grow it without bound.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass


def digest(content: str) -> str:
    """The freshness token. Content hash rather than mtime — cross-platform and
    robust, where mtime is unreliable on Windows and cloud-synced trees.

    It lives here rather than in the file tools because this module defines what
    a freshness token *is*; anything that wants to compare one has to spell it
    the same way, and two spellings would be a bug that only shows up as an edit
    sailing through a stale check."""
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


@dataclass
class FileState:
    content: str
    mtime: str          # opaque freshness token from the Cell (stat mtime, as text)
    shown_fully: bool = False
    """Whether the model was shown this file's entire text, as opposed to a
    window of it. Freshness only cares that the file was read; the
    unchanged-re-read shortcut additionally needs to know the model still *has*
    the content, which is false after a ranged read."""

    reported_change: str | None = None
    """The digest of the last *external* change already announced to the model.

    Separate from `mtime`, and that separation is the whole point. `mtime` stays
    at the value the model actually saw, so read-before-write keeps refusing the
    edit; this only stops the same change being announced on every subsequent
    turn. Folding the two would silence the announcement AND the refusal, which
    is the exact combination that lets a blind edit through."""


class FileStateCache:
    def __init__(self, max_entries: int = 100) -> None:
        self._cache: "OrderedDict[str, FileState]" = OrderedDict()
        self._max = max_entries

    @staticmethod
    def _norm(path: str) -> str:
        return path.replace("\\", "/").rstrip("/")

    def record(self, path: str, content: str, mtime: str, shown_fully: bool = True) -> None:
        key = self._norm(path)
        self._cache[key] = FileState(content, mtime, shown_fully)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """Forget every file. Called after compaction: the model's memory of file
        contents is now a summary's, not a transcript's, so read-before-write
        must make it look again rather than trust a read it can no longer see."""
        self._cache.clear()

    def get(self, path: str) -> FileState | None:
        key = self._norm(path)
        st = self._cache.get(key)
        if st is not None:
            self._cache.move_to_end(key)
        return st

    def tracked(self, limit: int | None = None) -> list[str]:
        """Paths worth re-checking, most recently used first.

        Bounded on purpose: whoever sweeps this has to touch the filesystem once
        per entry, and a file the model read forty turns ago is not what it is
        about to edit."""
        paths = list(reversed(self._cache))
        return paths[:limit] if limit is not None else paths

    def note_external_change(self, path: str, current: str) -> bool:
        """True the first time `path` is seen to differ from what the model read.

        Called by a sweep, never by a tool — a tool that writes records its own
        new state and so can never report itself as having changed underneath.
        Returns True once per distinct change: a file a formatter rewrote is
        worth one sentence, and worth another only if something rewrites it
        again.

        Looks the entry up WITHOUT promoting it. `get` is a use and reorders the
        LRU; a sweep is not a use, it is the harness checking up on everything
        at once. Promoting here would rewrite the recency order into sweep
        order on every command — and since the sweep walks newest-first, that
        order is inverted, so the least relevant files would end up at the front
        and the twenty this looks at next time would be the wrong twenty."""
        st = self._cache.get(self._norm(path))
        if st is None or current == st.mtime or current == st.reported_change:
            return False
        st.reported_change = current
        return True

    def freshness_error(self, path: str, current_mtime: str | None) -> str | None:
        """Return an explanatory error if `path` may not be edited yet, else None.
        current_mtime is the Cell's current mtime for the file (None if absent)."""
        st = self.get(path)
        if st is None:
            return (f"File {path!r} has not been read yet. Read it first before "
                    f"writing to it (read-before-write).")
        if current_mtime is not None and current_mtime != st.mtime:
            return (f"File {path!r} has been modified since you last read it. "
                    f"Read it again before editing to avoid clobbering changes.")
        return None
