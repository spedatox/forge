"""`telegram_send` — the agent deliberately reaching the owner mid-job.

Distinct from the completion notice in `gate/runner.py`, and the distinction is
the point. That one is the harness reporting that a job ended; this one is the
agent deciding something is worth interrupting a person for. Mark VI draws the
same line — its Tier-1 `send_telegram_message` skill sits beside, not instead
of, its notification path.

**This is an outward-facing action and is treated as one.** It reaches a human
on a device, it cannot be recalled, and nothing downstream will catch a mistake.
So: withheld unless configured, capped in length, and described in terms that
make the cost of over-using it explicit. The permission engine sees it as a
non-read-only tool, so an operator running in plan mode gets it denied along
with everything else that leaves a mark.

Send only — see `forge/notify.py` on why Forge never polls for updates.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge import notify
from forge.warden.tool import Tool, ToolContext, ToolResult

_MAX_CHARS = 3_000

_NOT_CONFIGURED = (
    "Telegram is not configured in this Forge (FORGE_TELEGRAM_TOKEN and "
    "FORGE_TELEGRAM_CHAT_ID). This is an operator setup task, not something you "
    "can fix — do not retry. If the owner needs to know something, put it in "
    "your final report instead."
)


class TelegramSendArgs(BaseModel):
    message: str = Field(
        description="What to tell the owner. Plain text, a few sentences at most.")


class TelegramSend(Tool):
    name = "telegram_send"
    description = (
        "Send the owner a short message on Telegram, right now, without waiting "
        "for the job to finish. Use it when something cannot wait for the final "
        "report: you are blocked and need a decision, you found something the "
        "owner would want to act on immediately, or a long job has reached a "
        "milestone they asked to be told about. Do NOT use it to narrate "
        "progress, to acknowledge instructions, or to deliver the final answer "
        "— the job already reports its own completion, and a second copy on "
        "their phone is noise. This reaches a person on a device and cannot be "
        "unsent; one message is almost always the right number."
    )
    Args = TelegramSendArgs
    # Not read-only: it leaves a mark outside the workspace. That is what makes
    # plan mode deny it, which is correct — a review pass must not message
    # anybody.
    READ_ONLY = False
    CONCURRENCY_SAFE = False

    async def call(self, args: TelegramSendArgs, ctx: ToolContext) -> ToolResult:
        agent_id = getattr(ctx, "agent_id", "") or ""
        if not notify.configured(agent_id):
            return ToolResult(_NOT_CONFIGURED, is_error=True)

        text = args.message.strip()
        if not text:
            return ToolResult("Nothing to send — the message was empty.", is_error=True)
        if len(text) > _MAX_CHARS:
            # Truncated rather than refused: the model already decided this was
            # worth interrupting a person for, and losing the message entirely
            # over its length serves nobody. The cap is about the phone screen.
            text = text[:_MAX_CHARS].rstrip() + "\n\n[…truncated]"

        if await notify.send(text, agent_id=agent_id):
            return ToolResult("Sent to the owner on Telegram.")
        return ToolResult(
            "Telegram accepted nothing — the bot or chat may be misconfigured, "
            "or the API is unreachable. Do not retry; include this in your "
            "final report instead.", is_error=True)
