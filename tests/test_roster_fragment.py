"""The agent-network fragment — how a peer learns it is not the only one.

Each peer runs its own system_prompt.md; Mark VI's shared agent-network section
never reaches it. So Optimus's prompt said nothing about Centurion and the
reverse, and the owner could hand the same job to both with neither the wiser.
The fragment closes that from the Forge registry — which already knows every
sibling folder — so a third agent joins every roster by existing, with no line
naming it. These tests hold: self is excluded, peers are named with their
domain, and the sentence that points at the channel tools appears only when
there is a channel to reach them through.
"""
from __future__ import annotations

from dataclasses import dataclass

from forge.agents.roster import network_fragment


@dataclass
class _Cfg:
    name: str
    domain: str = ""


class _Registry:
    """A stand-in for AgentRegistry: ids() + get(), nothing else is read."""

    def __init__(self, configs: dict[str, _Cfg]) -> None:
        self._configs = configs

    def ids(self) -> list[str]:
        return sorted(self._configs)

    def get(self, agent_id: str) -> _Cfg:
        return self._configs[agent_id]


def _registry() -> _Registry:
    return _Registry({
        "optimus": _Cfg("Optimus", "systems, code & infrastructure"),
        "centurion": _Cfg("Centurion", "cyber security"),
    })


# ── Who the peers are ────────────────────────────────────────────────────────

def test_it_names_the_other_agents_with_their_domain():
    fragment = network_fragment(_registry(), "optimus", has_channel=True)

    assert fragment is not None
    assert "Centurion" in fragment.text
    assert "cyber security" in fragment.text


def test_an_agent_is_not_told_who_it_is():
    """Self is excluded — the roster is the OTHER agents, and listing yourself as
    a colleague reads as a second, separate Optimus."""
    fragment = network_fragment(_registry(), "optimus", has_channel=True)

    assert "Optimus" not in fragment.text


def test_a_lone_agent_gets_no_fragment():
    """One agent has no network. A fragment that says 'you work alongside:' and
    then lists nobody is worse than silence."""
    solo = _Registry({"optimus": _Cfg("Optimus", "everything")})

    assert network_fragment(solo, "optimus", has_channel=True) is None


def test_a_third_agent_appears_in_every_roster_by_existing():
    """The identity-free promise: adding a folder is the whole change. Nothing in
    roster.py names an agent, so a new one is visible to the others for free."""
    three = _Registry({
        "optimus": _Cfg("Optimus", "systems"),
        "centurion": _Cfg("Centurion", "security"),
        "atlas": _Cfg("Atlas", "logistics"),
    })
    fragment = network_fragment(three, "optimus", has_channel=True)

    assert "Centurion" in fragment.text
    assert "Atlas" in fragment.text


# ── The channel sentence is conditional ──────────────────────────────────────

def test_it_points_at_the_channel_tools_only_when_there_is_a_channel():
    """Online, the fragment tells the agent HOW to coordinate. That sentence is
    the same shape as the memory block naming the memory tool — valid only when
    the tool is actually there."""
    online = network_fragment(_registry(), "optimus", has_channel=True)

    assert "read_agent_channel" in online.text
    assert "recall_conversations" in online.text


def test_it_names_no_tool_it_cannot_reach_offline():
    """Offline the peers are still real and worth knowing about, but the tools
    are withheld — naming them would teach the agent it had coordinated when it
    could not."""
    offline = network_fragment(_registry(), "optimus", has_channel=False)

    assert "Centurion" in offline.text                 # the roster still holds
    assert "read_agent_channel" not in offline.text    # the tools do not
    assert "recall_conversations" not in offline.text


# ── It is a shared fragment, and it is actually wired in ─────────────────────

def test_it_is_labelled_shared_so_it_sorts_after_identity():
    """A repo's conventions and the agent's own identity should be able to refine
    each other in a fixed order; 'shared' is where cross-cutting discipline sits."""
    from forge.agents.prompt import ORDER

    fragment = network_fragment(_registry(), "optimus", has_channel=True)
    assert fragment.kind == "shared"
    assert "shared" in ORDER


def test_the_runner_composes_it_into_every_job():
    import inspect

    from forge.gate import runner

    src = inspect.getsource(runner.run_job)
    assert "network_fragment(" in src
    # Built from the registry (holds even offline) and gated on the live channel.
    assert "has_channel=ctx.memory is not None" in src


def test_the_real_registry_produces_a_mutual_roster():
    """End to end on the shipped profiles: each of the two peers sees the other."""
    from forge.agents.registry import AgentRegistry

    registry = AgentRegistry.load()
    optimus_view = network_fragment(registry, "optimus", has_channel=True)
    centurion_view = network_fragment(registry, "centurion", has_channel=True)

    assert "Centurion" in optimus_view.text and "Optimus" not in optimus_view.text
    assert "Optimus" in centurion_view.text and "Centurion" not in centurion_view.text
