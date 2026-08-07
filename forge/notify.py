"""Telling the owner something happened when they are not at the terminal.

`tui/notify.py` answers the case where the operator is at the terminal and
looked away: a bell, an OSC notification, four bytes. This answers the case it
cannot — `forge connect` running as a systemd unit on a box the owner is not
logged into. A job finishes and the only trace is a journal line nobody reads.

    surface          operator is                       answer
    forge chat       at the terminal, looked away      bell / OSC   (tui/notify)
    forge connect    not at the terminal at all        Telegram     (here)

**The policy is inherited, not reinvented.** `tui/notify.MIN_SECONDS` already
decided when a completion is worth announcing: past thirty seconds, opt-out by
env, never for a job short enough that the owner was still watching. A second
threshold living here would drift from that one and the two surfaces would
disagree about the same job.

**Send only. Never `getUpdates`, never a webhook.** Mark VI already runs a bot
per agent and owns the inbound side — Optimus's webhook lands on that backend
and is proxied out through `core/external_proxy.py`. If this module also polled,
two processes would race the same update stream and messages would be delivered
twice or stolen outright. Forge speaks and does not listen; that is a
correctness constraint, not a scope decision, so it is stated here rather than
left as something a later reader might "fix".

Configuration, keyed the way Mark VI keys it — identity on the profile, secrets
in the environment:

    FORGE_TELEGRAM_TOKEN_<AGENT>   per-agent bot, uppercased agent id
    FORGE_TELEGRAM_TOKEN           fallback for a single-bot deployment
    FORGE_TELEGRAM_CHAT_ID         where to send
    FORGE_NO_TELEGRAM              off switch

Unconfigured is the normal case and is silent. Nothing here ever raises into a
job: a notification that kills the run it was reporting on is worse than no
notification.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("forge.notify")

_API = "https://api.telegram.org"
_TIMEOUT_S = 15.0

# Telegram's hard ceiling on a text message. Split on a paragraph boundary when
# one is near the end, so a chunk does not open mid-sentence.
_MAX_MESSAGE = 4096
_SPLIT_WINDOW = 400


def token_for(agent_id: str = "") -> str:
    """The bot token for this agent, or "".

    Per-agent first so the owner's contact list becomes the agent roster —
    Sentinel's alert arrives from @SentinelBot — falling back to a single token
    for deployments running one bot.
    """
    if agent_id:
        specific = os.environ.get(
            f"FORGE_TELEGRAM_TOKEN_{agent_id.upper().replace('-', '_')}", "")
        if specific.strip():
            return specific.strip()
    return os.environ.get("FORGE_TELEGRAM_TOKEN", "").strip()


def chat_id() -> str:
    return os.environ.get("FORGE_TELEGRAM_CHAT_ID", "").strip()


def configured(agent_id: str = "") -> bool:
    if os.environ.get("FORGE_NO_TELEGRAM"):
        return False
    return bool(token_for(agent_id) and chat_id())


def chunks(text: str, limit: int = _MAX_MESSAGE) -> list[str]:
    """Split a message at Telegram's ceiling, preferring a paragraph break.

    Pure, so the splitting is testable without a network. A hard slice at 4096
    routinely lands mid-word; looking back a few hundred characters for a blank
    line costs nothing and makes the seam invisible in the common case.
    """
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit - _SPLIT_WINDOW:
            cut = window.rfind("\n")
        if cut < limit - _SPLIT_WINDOW:
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


def _client():
    try:
        import httpx
    except ImportError:
        return None
    return httpx


async def send(text: str, *, agent_id: str = "") -> bool:
    """Send a message. True if it went. Never raises.

    Returns rather than throws for the same reason every tool in this harness
    does: the caller is mid-job or just finished one, and a failed notification
    is information, not a reason to lose the work it was announcing.
    """
    if not configured(agent_id) or not text.strip():
        return False
    httpx = _client()
    if httpx is None:
        logger.debug("telegram_skipped_no_httpx")
        return False

    token, chat = token_for(agent_id), chat_id()
    sent = False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for part in chunks(text):
                response = await client.post(
                    f"{_API}/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": part,
                          "disable_web_page_preview": True},
                )
                if response.status_code != 200:
                    # Logged at warning, not error: an unreachable bot is an
                    # operator problem and the job it belongs to succeeded.
                    logger.warning("telegram_send_failed",
                                   extra={"status": response.status_code})
                    return sent
                sent = True
    except Exception as e:  # noqa: BLE001 — see docstring
        logger.warning("telegram_send_errored", extra={"error": repr(e)})
        return sent
    return sent


async def send_document(path: str, caption: str = "", *, agent_id: str = "") -> bool:
    """Upload a file. True if it went. Never raises."""
    if not configured(agent_id):
        return False
    httpx = _client()
    if httpx is None:
        return False
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError as e:
        logger.warning("telegram_document_unreadable", extra={"error": repr(e)})
        return False

    name = os.path.basename(path) or "attachment"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S * 4) as client:
            response = await client.post(
                f"{_API}/bot{token_for(agent_id)}/sendDocument",
                data={"chat_id": chat_id(), "caption": caption[:1024]},
                files={"document": (name, payload)},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram_document_errored", extra={"error": repr(e)})
        return False
    return response.status_code == 200


async def job_finished(agent_id: str, task: str, summary: str,
                       seconds: float, ok: bool = True) -> bool:
    """Announce a completed job, if it ran long enough to be worth announcing.

    The threshold is `tui.notify.MIN_SECONDS` rather than one of this module's
    own — the two surfaces are answering the same question about the same job
    and must not disagree about whether it was long enough to mention.
    """
    from forge.tui.notify import MIN_SECONDS

    if seconds < MIN_SECONDS:
        return False
    head = "✅" if ok else "⚠️"
    # The task first, because after an hour away the owner needs to be told
    # WHICH job finished before anything about how it went.
    body = f"{head} {agent_id} finished\n\n{task.strip()[:300]}"
    if summary.strip():
        body += f"\n\n{summary.strip()}"
    return await send(body, agent_id=agent_id)
