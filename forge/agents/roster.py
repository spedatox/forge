"""The agent network, as a prompt fragment — how a peer learns it is not alone.

Each Forge peer runs its own `system_prompt.md` and nothing else. Mark VI's
in-process profiles assemble a shared `core/09_agent_network.md` section that
tells them the roster; the peers never receive it, so Optimus's prompt said
nothing about Centurion and Centurion's said nothing about Optimus. Two agents
serving one owner, each unaware the other existed — the owner could hand the
same job to both and neither would know to check.

This closes that gap the identity-free way the engine is built on: the roster is
read from the Forge `AgentRegistry` — which already discovers every
`<id>/profile.toml` and so already knows both peers — and rendered as a
`shared` fragment. Adding a third agent is still a folder and nothing else: it
appears in every other agent's roster automatically, with no line naming it
here. Self is excluded, because an agent does not need to be told who it is.

The fragment states two things and no more: WHO the peers are (always true, and
true offline — it comes from the Forge side, not Mark VI), and, only when there
is a live channel, that the shared record of their work is reachable with
`read_agent_channel` / `recall_conversations`. The tool sentence is conditional
for the same reason the tools themselves are withheld without a channel: naming a
tool the agent has not got teaches it to believe it already coordinated when it
could not (forge/tools/recall.py, forge/warden/toolsource.py).
"""
from __future__ import annotations

from forge.agents.prompt import PromptFragment


def network_fragment(registry, self_id: str, *, has_channel: bool) -> PromptFragment | None:
    """The other agents this one works alongside, or None when it has no peers.

    `registry` is the Forge AgentRegistry (or anything with `ids()` + `get()`);
    `self_id` is dropped from the roster. `has_channel` gates the one sentence
    that points at the channel tools, so an offline run still learns who its
    peers are without being told to use tools it was not given.
    """
    peers = [registry.get(aid) for aid in registry.ids() if aid != self_id]
    if not peers:
        return None

    lines = [
        "You are one of several agents that serve the same owner, each with its "
        "own domain and its own past work. You are not working alone. Alongside "
        "you:",
    ]
    for cfg in peers:
        domain = (getattr(cfg, "domain", "") or "").strip()
        lines.append(f"  - {cfg.name}" + (f" — {domain}" if domain else ""))

    if has_channel:
        lines.append(
            "The owner may hand the same work to any of you. Before starting "
            "something another agent may already have handled, and to pick up "
            "context they produced, read the shared network log with "
            "`read_agent_channel`; use `recall_conversations` to reach any past "
            "session across the whole roster, including your own. Don't duplicate "
            "or contradict another agent's work, and when something is squarely "
            "another agent's domain, say so rather than reaching into it."
        )
    else:
        lines.append(
            "The owner may hand the same work to any of you. Don't assume you are "
            "the only one who has touched a problem, and when something is "
            "squarely another agent's domain, say so rather than reaching into it."
        )

    return PromptFragment("shared:network", "\n".join(lines))
