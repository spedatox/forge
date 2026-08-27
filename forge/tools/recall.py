"""`recall_conversations` and `read_agent_channel` — Mark VI's memory of what
was SAID and what the OTHER agents have been doing.

The `memory` tool next door reaches the owner's memory *files*. These two reach
the other two things the injected block already tells the agent it can do and
the peer had no tool for:

  - **recall_conversations** — semantic recall over the owner's ENTIRE history,
    across every agent, by meaning rather than exact wording. This is the answer
    to "have we discussed X", "what did I decide about Y", "what was I saying
    about Z in June" — the past sessions a stateless peer would otherwise have
    no way back to, since Mark VI's database is the only place they survive.
  - **read_agent_channel** — the shared inter-agent log. It is how Optimus finds
    out what Centurion has been working on and the reverse: not a private note
    but the network's group channel, so a peer can pick up context another agent
    already produced instead of redoing it or contradicting it.

**Channels, not stores — the same rule the `memory` tool is built on.** Nothing
is embedded, indexed or cached here. The vectors, the FTS5 index and the agent
channel all live in Mark VI, which owns them; this sends one `memory_request`
over the peer socket and returns what came back. A peer keeping its own copy of
any of it would be the "memory that quietly forks" the whole design refuses:
Mark VI's database is the source of truth, and a second one would be a second
answer to the same question the first time either side changed.

**Read-only, and reached through the same frame the write path uses.** The
backend runs the SAME skills its in-process agents run (SemanticSearchSkill,
AgentChannelSkill) — so an external agent recalls and reads exactly as Sentinel
or Atomix does, with no parallel implementation to drift. See
app/services/peer_memory.py on Mark VI's side.

Withheld entirely when there is no channel to Mark VI, on the rule the `memory`
tool follows: the standalone TUI has no backend, so these are absent rather than
present and failing on every call.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_NO_CHANNEL = (
    "There is no connection to Mark VI on this run, so the conversation history "
    "and the agent channel cannot be reached. This is the mode you are in, not a "
    "fault you can retry — say so plainly if it matters, and do not claim to have "
    "recalled anything or checked what another agent did."
)

# A recall result or a channel window is background for the turn, not the turn.
# Same cap and same reason as the `memory` tool and web_fetch: a long transcript
# must not evict the actual work it was fetched to inform.
MAX_CHARS = 20_000


async def _ask(ctx: ToolContext, payload: dict[str, Any]) -> ToolResult:
    """One read over the memory channel. Shared by both tools: the channel guard,
    the error passthrough and the truncation are identical, and the only thing
    that differs is the `skill` and its arguments the caller already built."""
    channel = getattr(ctx, "memory", None)
    if channel is None:
        return ToolResult(_NO_CHANNEL, is_error=True)

    reply = await channel.command(payload)
    if not reply.ok:
        # A backend that could not run the read at all. Its message is written
        # for the model; pass it through as the error it is rather than letting
        # an empty recall read as "nothing was found".
        return ToolResult(reply.text or "The memory backend returned no answer.",
                          is_error=True)

    text = reply.text or ""
    if len(text) > MAX_CHARS:
        return ToolResult(
            f"{text[:MAX_CHARS]}\n\n[… truncated at {MAX_CHARS:,} of "
            f"{len(text):,} characters. This is the head of the result, not all "
            f"of it — narrow the query or lower the limit for the rest.]")
    return ToolResult(text)


class RecallArgs(BaseModel):
    query: str = Field(
        description="What to recall, in natural language — a topic, question, or "
                    "past decision. Meaning-based, so it need not match the "
                    "original wording.")
    after: str | None = Field(
        default=None,
        description="Optional: only exchanges on/after this date (YYYY-MM-DD). "
                    "Combine with the query to find the MOST RECENT discussion "
                    "of a topic.")
    before: str | None = Field(
        default=None,
        description="Optional: only exchanges on/before this date (YYYY-MM-DD).")
    agent_id: str | None = Field(
        default=None,
        description="Optional: only exchanges from this agent's conversations "
                    "(optimus, centurion, speda, …). Leave unset to search the "
                    "whole roster, including your own past sessions.")
    context_window: int | None = Field(
        default=None,
        description="Turns to include either side of each hit (default 2, max 4).")
    limit: int | None = Field(
        default=None,
        description="Max matching exchanges to return (default 8, max 20).")


class RecallConversations(Tool):
    name = "recall_conversations"
    description = (
        "Search the owner's ENTIRE conversation history — across every agent, not "
        "just your own — by MEANING rather than exact wording, optionally narrowed "
        "to a date range or one agent. Use it for conceptual or fuzzy recall of a "
        "PAST SESSION: 'have we discussed X before', 'what did I decide about Y', "
        "'what was I working on in June', or to reconstruct something the owner "
        "mentioned earlier that is not in this conversation. You are stateless "
        "between turns and this is your only way back to a past session — Mark VI's "
        "database is where they live. Do NOT use it for the owner's DISTILLED facts "
        "(preferences, people, projects) — that is the `memory` tool — and NOT to "
        "read your current conversation, which is already in front of you. Returns "
        "matching exchanges as snippets with the turns either side of each hit, "
        "grouped by conversation and tagged with session, agent and date."
    )
    Args = RecallArgs
    READ_ONLY = True
    # Two reads never interfere, so a recall can share a batch with other reads.
    CONCURRENCY_SAFE = True

    async def call(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        payload: dict[str, Any] = {"skill": "recall_conversations"}
        payload.update({k: v for k, v in args.model_dump().items() if v is not None})
        return await _ask(ctx, payload)


class AgentChannelArgs(BaseModel):
    limit: int | None = Field(
        default=None,
        description="How many exchanges to show (default 20, max 60).")
    agent: str | None = Field(
        default=None,
        description="Optional agent_id — only exchanges involving this agent "
                    "(e.g. the other Forge peer). Omit for all network traffic.")


class ReadAgentChannel(Tool):
    name = "read_agent_channel"
    description = (
        "Read the agent network's group channel — the shared log of inter-agent "
        "dispatches and replies across the whole suite, rendered oldest-first like "
        "a chat scrollback. Use it to find out what the OTHER agents have been "
        "doing — Optimus and Centurion share this network, so this is how each sees "
        "the other's recent work — before starting a task another agent may already "
        "have handled, or to pick up context from earlier network traffic. Do NOT "
        "use it to recall your own conversations with the owner — that is "
        "`recall_conversations`. Returns the channel transcript, optionally filtered "
        "to one agent, or a note that the channel is empty."
    )
    Args = AgentChannelArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: AgentChannelArgs, ctx: ToolContext) -> ToolResult:
        payload: dict[str, Any] = {"skill": "read_agent_channel"}
        payload.update({k: v for k, v in args.model_dump().items() if v is not None})
        return await _ask(ctx, payload)
