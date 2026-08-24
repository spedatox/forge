"""remember_about_owner — a note about the owner, captured with no network.

Sits deliberately outside the `memory` group and its `without_memory_tools`
gate (forge/warden/toolsource.py): `memory` is a live channel to Mark VI and
is withheld the moment that channel is gone, but this tool's entire reason to
exist is the case `memory` cannot cover — a session running with no
connection at all. It writes one line to a local queue
(forge/agents/owner_memory.py's `pending_observations.jsonl`) and returns
immediately; nothing here talks to Mark VI.

**One-way, and it says so.** The queued line reaches Mark VI's memory the next
time this peer connects (flushed in forge/gate/peer.py right after
`_register()`), where Orion reviews it in its own nightly audit alongside
every other agent's observations — there is no read-back, no confirmation
that it "took", and no way for this or any later session to see it again
through this tool. That is intentional: deciding what a fact means, whether
it merges with something already known, or whether it contradicts an existing
one is Mark VI's job, not a queue's.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_DOMAINS = ("biography", "state")


class RememberAboutOwnerArgs(BaseModel):
    content: str = Field(
        description="The fact, as ONE self-contained sentence, understandable "
                    "without the session it came from.")
    domain: str = Field(
        default="state",
        description="'biography' for something durable and lasting about who he "
                    "is. 'state' for something true of his life right now. "
                    "Defaults to 'state'.")


class RememberAboutOwner(Tool):
    name = "remember_about_owner"
    description = (
        "Note one durable fact you have learned about the owner, for Mark VI's memory — "
        "works with or without a connection right now, unlike the `memory` tool. Use it "
        "when something worth keeping emerges in a session: a stated preference, a "
        "constraint, a decision, something true of his life at the moment. It is captured "
        "locally and immediately; it reaches Mark VI's memory (and Orion's own review) the "
        "next time this peer connects. Do NOT expect it to be searchable or visible "
        "anywhere in THIS session — it is write-only with no read-back. Do NOT use it for "
        "anything about the current repository or task (that belongs to this session, not "
        "the owner's memory), or for anything transient you would not want resurfacing "
        "months from now. Returns a plain confirmation that it was queued, nothing more."
    )
    Args = RememberAboutOwnerArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = True   # a local append; nothing here reads what it wrote
    DESTRUCTIVE = False

    async def call(self, args: RememberAboutOwnerArgs, ctx: ToolContext) -> ToolResult:
        # Imported here, not at module level: forge.agents (which owns
        # owner_memory) imports forge.agents.registry, which imports this
        # package (forge.tools) to resolve tool groups — a module-level import
        # here would close that loop while forge.tools is still initializing.
        from forge.agents import owner_memory

        domain = args.domain if args.domain in _DOMAINS else "state"
        owner_memory.queue_observation(args.content, domain=domain)
        return ToolResult(
            "Noted locally. It will reach Mark VI's memory the next time this "
            "peer connects — there is nothing further to check here."
        )
