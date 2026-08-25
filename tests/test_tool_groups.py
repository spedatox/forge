"""A tool nobody can be given is a tool that does not exist.

`hisar_list`, `hisar_read`, `hisar_deposit` and `telegram_send` were written,
documented, unit-tested, and filtered out when the deployment could not serve
them — and never once reached an agent, because the wiring that makes a group
NAMEABLE was never done. `_TOOL_GROUPS` had no `hisar` and no `notify`, so a
profile had no word for either, so neither profile asked for them.

Nothing failed. `_resolve_tools` rejects a group name it does not know, which
catches a profile asking for something that is not there. There was no check the
other way round — a group that exists and that nothing can ask for — and the two
look identical from inside the registry: a short tool list either way.

That is the shape this file exists to catch, because it is silent by
construction. A dead tool produces no error, no warning, and no failing test.
It produces an agent that quietly cannot do something, which nobody notices
until they go looking for why.
"""
from __future__ import annotations

import pytest

from forge.agents.registry import _TOOL_GROUPS, AgentRegistry
from forge.tools import (
    ALL_TOOLS,
    ASK_TOOLS,
    CODING_TOOLS,
    HISAR_TOOLS,
    MEMORY_TOOLS,
    NAV_TOOLS,
    NOTIFY_TOOLS,
    SECURITY_TOOLS,
    WEB_TOOLS,
)


def _offered() -> set[str]:
    """Every tool some configured agent can actually be given."""
    registry = AgentRegistry.load()
    return {name for agent_id in registry.ids()
            for name in registry.get(agent_id).tool_names}


# ── The invariant that was broken ────────────────────────────────────────────

def test_no_tool_is_defined_but_unreachable():
    """The bug, stated as a rule.

    Four tools sat in ALL_TOOLS for months without any agent being able to hold
    one. Adding a tool and forgetting to route it to somebody is the easiest
    possible mistake here and the hardest to see, because the result looks
    exactly like a deliberately narrow allowlist."""
    unreachable = sorted(set(ALL_TOOLS) - _offered())

    assert not unreachable, (
        f"defined but reachable by no agent: {', '.join(unreachable)}. "
        f"Either add the group to registry._TOOL_GROUPS and name it in a "
        f"profile, or remove the tool — a door nobody can open is not a "
        f"narrower default, it is dead code that reads as a feature.")


@pytest.mark.parametrize("group", [
    pytest.param(HISAR_TOOLS, id="hisar"),
    pytest.param(NOTIFY_TOOLS, id="notify"),
    pytest.param(WEB_TOOLS, id="web"),
    pytest.param(MEMORY_TOOLS, id="memory"),
])
def test_every_standalone_group_is_nameable_by_a_profile(group):
    """A group is only a group if a profile has a word for it.

    NAV_TOOLS and ASK_TOOLS are deliberately absent — see the test below."""
    names = {cls.name for cls in group}
    nameable = {name for entry in _TOOL_GROUPS.values() for name in entry}

    assert names <= nameable, f"{sorted(names - nameable)} cannot be asked for"


def test_the_building_blocks_are_still_building_blocks():
    """NAV_TOOLS and ASK_TOOLS are NOT standalone groups, and their absence
    from `_TOOL_GROUPS` is the deliberate case the test above must not punish.

    Navigation is a component of `coding` and `security` rather than a
    capability anybody takes alone, and ask_operator is in CODING_TOOLS on the
    stated argument that reaching a fork you should not pick by yourself is not
    an optional extra for real work. Pinned so that "make everything a group"
    is a decision somebody makes rather than something this file quietly
    enforces."""
    assert "nav" not in _TOOL_GROUPS
    assert "ask" not in _TOOL_GROUPS

    for building_block in (NAV_TOOLS, ASK_TOOLS):
        names = {cls.name for cls in building_block}
        assert names <= {cls.name for cls in CODING_TOOLS}

    assert {cls.name for cls in NAV_TOOLS} <= {cls.name for cls in SECURITY_TOOLS}


# ── What a configured deployment actually offers ─────────────────────────────

def _configure_vault(monkeypatch):
    monkeypatch.setenv("HISAR_MACHINE_TOKEN", "test-token")


def _configure_telegram(monkeypatch):
    monkeypatch.delenv("FORGE_NO_TELEGRAM", raising=False)
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("FORGE_TELEGRAM_CHAT_ID", "12345")


def _resolved(agent_id: str = "optimus") -> dict:
    """The toolset as a job would see it, through the availability filter."""
    from forge.warden.toolsource import resolve_optional

    config = AgentRegistry.load().get(agent_id)
    return resolve_optional({name: ALL_TOOLS[name]() for name in config.tool_names})


def test_a_configured_vault_is_actually_offered(monkeypatch):
    """The end of the chain, which is the only part that was ever broken. Every
    link before it — the tools, the group, the filter — already worked."""
    _configure_vault(monkeypatch)

    tools = _resolved()

    for name in ("hisar_list", "hisar_read", "hisar_deposit"):
        assert name in tools, name


def test_a_configured_bot_is_actually_offered(monkeypatch):
    _configure_telegram(monkeypatch)

    assert "telegram_send" in _resolved()


def test_an_unconfigured_vault_is_still_withheld(monkeypatch):
    """The filter has to keep working now that something reaches it. Without a
    token every call is a 401, which a model reads as a transient fault worth
    retrying — so it burns turns on a door that was never going to open."""
    monkeypatch.delenv("HISAR_MACHINE_TOKEN", raising=False)

    tools = _resolved()

    for name in ("hisar_list", "hisar_read", "hisar_deposit"):
        assert name not in tools, name


def test_an_unconfigured_bot_is_still_withheld(monkeypatch):
    monkeypatch.delenv("FORGE_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_TELEGRAM_CHAT_ID", raising=False)

    assert "telegram_send" not in _resolved()


def test_a_configured_push_token_offers_git_push(monkeypatch):
    monkeypatch.setenv("FORGE_GIT_TOKEN", "ghp_test")
    assert "git_push" in _resolved()


def test_an_unconfigured_push_token_withholds_git_push(monkeypatch):
    """Without a token every push is an auth failure the model retries — so the
    tool is withheld, exactly like the vault, rather than offered and failing."""
    monkeypatch.delenv("FORGE_GIT_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_GIT_TOKEN_OPTIMUS", raising=False)
    assert "git_push" not in _resolved()


# ── Who gets what, stated so a change to it is deliberate ────────────────────

def test_the_peer_can_reach_the_vault_and_the_owner(monkeypatch):
    """Optimus is the agent both were built for: `forge connect` runs on a box
    nobody is logged into, and its Cell workspace does not survive the job."""
    _configure_vault(monkeypatch)
    _configure_telegram(monkeypatch)

    tools = _resolved("optimus")

    assert {"hisar_list", "hisar_read", "hisar_deposit", "telegram_send"} <= set(tools)


def test_the_security_agent_has_the_ecosystem_tools(monkeypatch):
    """Centurion is the main agent now, not a narrow specialist. He takes hisar
    (evidence vault for reports and pcaps) and notify (reach the operator mid-
    engagement) because the operator explicitly widened his reach — a security
    agent without durable output or a way to alert the operator is half-armed."""
    _configure_vault(monkeypatch)
    _configure_telegram(monkeypatch)

    tools = _resolved("centurion")

    assert {"hisar_list", "hisar_read", "hisar_deposit"} <= set(tools)
    assert "telegram_send" in tools


def test_the_two_groups_stay_separate():
    """The commit that created them says why: a profile takes one, both or
    neither on purpose. Merging them would make a line to the owner's phone
    something you get by wanting to read a file."""
    assert _TOOL_GROUPS["hisar"] != _TOOL_GROUPS["notify"]
    assert "telegram_send" not in _TOOL_GROUPS["hisar"]
    assert not set(_TOOL_GROUPS["notify"]) & set(_TOOL_GROUPS["hisar"])


def test_neither_is_folded_into_coding():
    """The whole reason they are groups. A coding agent should not acquire the
    owner's documents by asking for a file editor."""
    coding = {cls.name for cls in CODING_TOOLS}

    assert not coding & {cls.name for cls in HISAR_TOOLS}
    assert not coding & {cls.name for cls in NOTIFY_TOOLS}
