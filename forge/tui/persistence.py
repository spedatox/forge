"""Sessions that survive the terminal closing.

A conversation currently lives in memory and nowhere else. Close the window
during a long refactor and it is gone — not the files, which are on disk, but
everything about why they look like that: what was tried, what was rejected,
what the plan was. The work that took the longest to build up is the work that
vanishes most completely.

The peer path never had this problem, because Mark VI's database holds the
transcript and resends it. Local sessions had nothing.

Written after each completed turn. A turn is the natural unit: it is atomic
from the operator's point of view, and it is the point at which the transcript
is known to be well-formed — mid-turn there can be a tool_use with no result,
which is exactly the shape that cannot be replayed.

Stored per workspace, under `.forge/sessions/`. The same reasoning as input
history: a conversation about one repository is noise in another, and a shared
store would offer the operator a list of other projects' work to resume.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
KEEP_SESSIONS = 40
"""Older ones are pruned. A resume list nobody can read is not a feature, and
these are transcripts — they are not small."""

_TITLE_CHARS = 72


def sessions_dir(workspace: Path) -> Path:
    return workspace / ".forge" / "sessions"


def new_id() -> str:
    """Sortable, readable, and unique enough for one operator's machine."""
    return f"{datetime.now():%Y%m%d-%H%M%S}"


@dataclass(frozen=True)
class SavedSession:
    """A stored conversation, as the resume list needs to describe it."""
    id: str
    path: Path
    title: str
    agent_id: str
    model_ref: str
    turns: int
    messages: int
    updated: float

    @property
    def age(self) -> str:
        seconds = max(0, time.time() - self.updated)
        if seconds < 90:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86_400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86_400)}d ago"


def _title_from(messages: list[dict[str, Any]]) -> str:
    """The first thing the operator asked, which is what they will recognise.

    Not a generated summary: naming a session costs a model call, and a wrong
    title is worse than a blunt one when the whole job is picking the right
    conversation out of a list.
    """
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        text = " ".join(text.split())
        if text:
            return text[:_TITLE_CHARS] + ("…" if len(text) > _TITLE_CHARS else "")
    return "(no prompt)"


def save(session: Any, session_id: str) -> Path | None:
    """Write the session. Returns the path, or None if it could not be written.

    Never raises: losing a save is a lost resume, while an exception here would
    lose the turn that was just completed.
    """
    try:
        directory = sessions_dir(session.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{session_id}.json"
        payload = {
            "version": FORMAT_VERSION,
            "id": session_id,
            "agent_id": session.cfg.agent_id,
            "model_ref": session.model_ref,
            "workspace": str(session.workspace),
            "turns": session.turns,
            "updated": time.time(),
            "title": _title_from(session.messages),
            "messages": session.messages,
        }
        # Written to a temporary file and moved, so an interrupted save cannot
        # leave a half-written transcript that fails to parse on resume.
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(target)
        _prune(directory)
        return target
    except Exception as e:  # noqa: BLE001
        logger.warning("session_save_failed", extra={"error": repr(e)})
        return None


def _prune(directory: Path, keep: int = KEEP_SESSIONS) -> None:
    try:
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def listing(workspace: Path, limit: int = 20) -> list[SavedSession]:
    """Recent sessions, newest first. Unreadable files are skipped, not fatal."""
    directory = sessions_dir(workspace)
    if not directory.is_dir():
        return []
    out: list[SavedSession] = []
    for path in sorted(directory.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("version") != FORMAT_VERSION:
            continue
        out.append(SavedSession(
            id=str(data.get("id") or path.stem),
            path=path,
            title=str(data.get("title") or "(untitled)"),
            agent_id=str(data.get("agent_id") or "?"),
            model_ref=str(data.get("model_ref") or "?"),
            turns=int(data.get("turns") or 0),
            messages=len(data.get("messages") or []),
            updated=float(data.get("updated") or path.stat().st_mtime),
        ))
        if len(out) >= limit:
            break
    return out


def load(workspace: Path, session_id: str) -> list[dict[str, Any]] | None:
    """The stored transcript, or None when it cannot be read."""
    path = sessions_dir(workspace) / f"{session_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("version") != FORMAT_VERSION:
        return None
    messages = data.get("messages")
    return messages if isinstance(messages, list) else None


def resolve(workspace: Path, reference: str) -> SavedSession | None:
    """Accept a list position (`1`), a full id, or a unique id prefix.

    Position is what the operator just read off the screen; the id is what
    survives them running the list again later. Supporting both costs four
    lines and removes a step from the common case.
    """
    entries = listing(workspace)
    if not entries:
        return None
    reference = reference.strip()
    if not reference:
        return entries[0]
    if reference.isdigit():
        index = int(reference) - 1
        return entries[index] if 0 <= index < len(entries) else None
    exact = [e for e in entries if e.id == reference]
    if exact:
        return exact[0]
    prefixed = [e for e in entries if e.id.startswith(reference)]
    return prefixed[0] if len(prefixed) == 1 else None
