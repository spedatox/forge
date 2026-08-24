"""What Mark VI knows about the owner, and what survives losing Mark VI.

Three jobs, deliberately in one place because the second and third are both
built on the same disk cache as the first.

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
a SNAPSHOT, labelled with its age, and the snapshot file itself is never
written back to. Orion also pushes a fresh copy here once a night, unprompted
(`owner_memory_sync`, see forge/gate/peer.py), so the snapshot is never more
than one audit cycle stale even on a night this peer never ran a connected job.

**The one write-back path.** A session captures something about the owner
worth keeping (`queue_observation`) into a SEPARATE local file — the snapshot
above stays read-only. Queuing needs no connection at all; what reaches Mark VI
and when is `flush_pending`'s job, run once this peer is connected again. This
is still one-way in spirit: nothing here decides whether the fact survives,
merges with something else, or contradicts an existing one — that judgement
happens on Mark VI's side, in Orion's own audit, on its own schedule. Nothing
merges silently here, because a memory that quietly forks is worse than one
that is briefly absent.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_NAME = "owner_memory.md"
PENDING_NAME = "pending_observations.jsonl"

# Guards the prompt against a runaway memory file. Mark VI already bounds what
# it preloads; this is a second fence, not the primary one.
MAX_CHARS = 24_000


def _home() -> Path:
    root = os.environ.get("FORGE_HOME")
    return Path(root) if root else Path.home() / ".forge"


def snapshot_path() -> Path:
    """Where the snapshot lives. Per-user, not per-workspace: it describes the
    owner, who is the same person in every repository they open."""
    return _home() / SNAPSHOT_NAME


def pending_path() -> Path:
    """Where locally-queued, not-yet-sent observations live. Same per-user
    scope as the snapshot, for the same reason."""
    return _home() / PENDING_NAME


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
        f"files in this mode — but `remember_about_owner` still works: it queues "
        f"a note locally and it reaches Mark VI the next time this peer "
        f"connects, without waiting on a session import."
    )
    return PromptFragment(
        "owner memory (offline snapshot)", f"{header}\n\n{body[:MAX_CHARS]}")


def queue_observation(content: str, *, domain: str = "state", level: str = "explicit") -> None:
    """Queue one fact about the owner locally — no connection required.

    Best-effort exactly like `remember()`: a note that fails to write is lost
    rather than blocking whatever noticed it, the same trade this module
    already makes for the read-side cache. Append-only; `flush_pending` is the
    only thing that ever removes a line.
    """
    text = (content or "").strip()
    if not text:
        return
    entry = {
        "content": text,
        "level": level,
        "domain": domain,
        "queued_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        path = pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.debug("owner_memory_queue_write_failed: %s", e)


async def flush_pending(channel: Any) -> int:
    """Replay every queued observation through `channel` (a MemoryChannel),
    keeping only the lines that were not actually recorded.

    Called right after this peer reconnects (forge/gate/peer.py), so a note
    captured fully offline reaches Mark VI's record the next time anyone is
    listening, rather than waiting on a session import that may never happen.
    Returns how many were sent successfully.

    `ok=False` on the reply means the command could not run at all (per
    peer_memory.py's contract) — that line stays queued for the next flush.
    `ok=True` means Mark VI's side ran it, even if what ran was a refusal;
    retrying a refusal changes nothing, so that line is dropped either way.
    """
    path = pending_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return 0

    sent = 0
    remaining: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a corrupted line is dropped, not retried forever
        payload = {
            "skill": "record_observation",
            "content": entry.get("content", ""),
            "level": entry.get("level", "explicit"),
            "domain": entry.get("domain", "state"),
        }
        try:
            reply = await channel.command(payload)
        except Exception as e:  # noqa: BLE001 — a bad flush must not lose the line
            logger.debug("owner_memory_flush_failed: %s", e)
            remaining.append(line)
            continue
        if reply.ok:
            sent += 1
        else:
            remaining.append(line)

    try:
        if remaining:
            path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("owner_memory_queue_rewrite_failed: %s", e)

    if sent:
        logger.info("owner_memory_flushed", extra={"sent": sent, "remaining": len(remaining)})
    return sent
