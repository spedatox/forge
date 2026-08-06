"""What Mark VI knows about the owner, and what survives losing Mark VI.

Two jobs, deliberately in one place because the second is a cache of the first.

**Connected.** Every `chat_request` carries the same memory block Mark VI puts
into its own agents' system prompts. Before that was sent, an external peer ran
the owner's turns knowing the conversation and nothing about the owner — every
standing preference and durable fact stopped at the socket. It arrives as a
labelled fragment rather than as the system prompt, so Mark VI keeps ownership
of identity and the Forge keeps its own.

**Offline.** The architecture is peer-only: identity, history and memory live
in Mark VI, and the peer is hands rather than a brain. That is the right call —
one brain cannot be in two places without eventually disagreeing with itself —
but it means an unreachable backend leaves the hands with nothing driving them.

So each block that arrives is written to disk, and the standalone TUI loads the
most recent one. That is a countermeasure, not a second brain: it is explicitly
a SNAPSHOT, labelled with its age, and it is never written back to. An offline
session is its own session and is imported to Mark VI as a new one — nothing
merges silently, because a memory that quietly forks is worse than one that is
briefly absent.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPSHOT_NAME = "owner_memory.md"

# Guards the prompt against a runaway memory file. Mark VI already bounds what
# it preloads; this is a second fence, not the primary one.
MAX_CHARS = 24_000


def snapshot_path() -> Path:
    """Where the snapshot lives. Per-user, not per-workspace: it describes the
    owner, who is the same person in every repository they open."""
    root = os.environ.get("FORGE_HOME")
    base = Path(root) if root else Path.home() / ".forge"
    return base / SNAPSHOT_NAME


def remember(block: str) -> None:
    """Cache the latest block. Best-effort: failing to write a convenience
    cache must never fail the turn that happened to carry it."""
    text = (block or "").strip()
    if not text:
        return
    try:
        path = snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        path.write_text(f"<!-- captured {stamp} -->\n{text}\n", encoding="utf-8")
    except OSError as e:
        logger.debug("owner_memory_snapshot_write_failed: %s", e)


def _read_snapshot() -> tuple[str, str] | None:
    """(captured_at, body), or None when there is no usable snapshot."""
    try:
        raw = snapshot_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    stamp = ""
    body = raw
    if raw.startswith("<!-- captured "):
        head, _, rest = raw.partition("\n")
        stamp = head.removeprefix("<!-- captured ").removesuffix("-->").strip()
        body = rest
    body = body.strip()
    return (stamp, body) if body else None


def _age(stamp: str) -> str:
    """How stale, in words. A bare timestamp asks the model to do date
    arithmetic mid-turn, which it does badly and silently."""
    if not stamp:
        return "unknown age"
    try:
        captured = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return "unknown age"
    now = _dt.datetime.now(captured.tzinfo) if captured.tzinfo else _dt.datetime.now()
    days = (now - captured).days
    if days <= 0:
        return "captured today"
    if days == 1:
        return "captured yesterday"
    return f"captured {days} days ago"


def live_fragment(block: str):
    """The block that came over the wire this turn, as a prompt fragment."""
    from forge.agents.prompt import PromptFragment

    text = (block or "").strip()
    if not text:
        return None
    return PromptFragment("owner memory (live from Mark VI)", text[:MAX_CHARS])


def offline_fragment():
    """The cached block, for a standalone run with no backend to ask.

    Labelled as a snapshot and dated on purpose. An agent told stale facts
    without being told they are stale will state them as current, which is the
    failure mode this whole cache would otherwise introduce.
    """
    from forge.agents.prompt import PromptFragment

    found = _read_snapshot()
    if found is None:
        return None
    stamp, body = found
    header = (
        f"You are running OFFLINE, without Mark VI. What follows is a snapshot "
        f"of the owner's memory, {_age(stamp)}. Treat it as background that may "
        f"be out of date rather than as current fact, and do not claim to have "
        f"checked anything in it. You cannot read or write the owner's memory "
        f"in this mode; nothing you learn here will reach Mark VI until this "
        f"session is imported."
    )
    return PromptFragment(
        "owner memory (offline snapshot)", f"{header}\n\n{body[:MAX_CHARS]}")
