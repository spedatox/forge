"""`ask_operator` — putting a question to the person, mid-job.

The gate already lets the agent ask for *permission*. This is the other half:
asking for a **decision or an opinion** when the work has reached a fork the
agent should not pick unilaterally. Prototyping on a server is exactly where
that bites — nobody is watching, the agent hits "REST or WebSocket?", and its
choices today are guess silently or stop.

**Not permission, and the difference is the failure direction.** An unanswered
permission request is a no, because absence of an operator must never become
consent. An unanswered question is *not* a no — there is nothing to refuse, and
blocking the work because nobody was around to have an opinion turns a courtesy
into a deadlock. So this degrades to "decide it yourself and say that you did",
which `oracle.Reply.guidance` spells out.

**Rationed by description, not by code.** An agent that asks about everything is
worse than one that asks about nothing: the first trains the owner to stop
reading, and then the one question that mattered goes unread too. There is no
counter enforcing that — a limit would fire on the wrong question as often as
the right one — so the description carries the whole of it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.oracle import has_consult
from forge.warden.tool import Tool, ToolContext, ToolResult

_NO_ORACLE = (
    "There is no operator channel on this job, so nobody can be asked. Do not "
    "retry. Choose the option you judge best, proceed, and state in your final "
    "report which way you went and that you decided it yourself."
)


class AskOperatorArgs(BaseModel):
    question: str = Field(
        description="What you need to know, in one or two sentences. State the "
                    "fork plainly and say what you would do by default.")
    options: list[str] = Field(
        default_factory=list,
        description="The candidate answers, if the question is a choice between "
                    "known alternatives. Leave empty for an open question.")


class AskOperator(Tool):
    name = "ask_operator"
    description = (
        "Ask the owner a question and wait for their answer. Use it at a fork "
        "you should not pick alone: a design decision with real consequences, "
        "an ambiguity in the request where the two readings lead to different "
        "work, or a judgement call about scope. Say what you would do by "
        "default so a busy owner can just agree.\n"
        "Do NOT use it to confirm you understood, to report progress, to ask "
        "permission for an action (the gate does that on its own), or for "
        "anything you could settle by reading the code. Asking costs the owner "
        "an interruption and costs you the wait; an agent that asks about "
        "everything gets ignored about everything. If no answer comes back, "
        "decide for yourself and say so in your report — do not ask twice and "
        "do not stall."
    )
    Args = AskOperatorArgs
    # Read-only in the sense the permission engine cares about: it changes
    # nothing in the workspace. Plan mode should still allow it — asking what to
    # build is exactly the kind of thing a review pass is for.
    READ_ONLY = True
    # Never batched. Two questions arriving at once is the shape that trains an
    # owner to stop reading them.
    CONCURRENCY_SAFE = False

    async def call(self, args: AskOperatorArgs, ctx: ToolContext) -> ToolResult:
        oracle = getattr(ctx, "oracle", None)
        if oracle is None or not has_consult(oracle):
            return ToolResult(_NO_ORACLE, is_error=True)

        question = args.question.strip()
        if not question:
            return ToolResult("The question was empty.", is_error=True)

        reply = await oracle.consult(question, args.options or None)
        # Not is_error even when unanswered: nothing failed. The model is being
        # told to proceed, and flagging that as an error would push it toward
        # retrying the one thing the guidance says not to retry.
        return ToolResult(reply.guidance)
