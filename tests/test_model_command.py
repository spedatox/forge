"""`/model` — switching what the next turn runs on.

The switch itself is one assignment; what is worth pinning down is everything
around it. A ref typed by hand is not checked against the catalog (a model
released this morning must be usable this morning). A machine with no keys gets
told which env var to set rather than an empty list. And the picker's selection
has to actually reach the session, because a switch that silently does nothing
looks exactly like a switch that worked.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.model.catalog import ModelInfo, ProviderCatalog
from forge.tui import commands
from forge.tui.commands import REGISTRY, resolve


class _Ledger:
    context_limit = 200_000


class _Cfg:
    agent_id = "optimus"
    permission_mode = "act"


class _Session:
    def __init__(self, model_ref="deepseek:deepseek-v4-pro"):
        self.cfg = _Cfg()
        self.model_ref = model_ref
        self.ledger = _Ledger()


def _run(args: str, session):
    cmd, _ = resolve("/model")
    return asyncio.run(cmd.run(args, session))


@pytest.fixture
def catalogued(monkeypatch):
    """Stand in for the network: a fixed catalog and a scripted pick."""
    def _install(groups, chooses=None):
        async def _catalog(settings, refresh=False, timeout=8.0):
            return groups

        async def _pick(groups_, title="", current=""):
            _pick.saw = groups_
            return chooses
        _pick.saw = []

        monkeypatch.setattr("forge.model.catalog.catalog", _catalog)
        monkeypatch.setattr("forge.tui.picker.pick", _pick)
        return _pick
    return _install


def test_the_command_and_its_alias_are_registered():
    assert "model" in REGISTRY
    assert resolve("/models")[0] is resolve("/model")[0]


# ── switching by name ───────────────────────────────────────────────────────


def test_a_typed_ref_switches_the_session():
    session = _Session()
    out = _run("openai:gpt-5.1", session)
    assert session.model_ref == "openai:gpt-5.1"
    assert "deepseek:deepseek-v4-pro" in out.text and "openai:gpt-5.1" in out.text


def test_an_unknown_ref_is_not_second_guessed():
    """The provider is a better referee than a cached list: a model released
    this morning should be usable this morning."""
    session = _Session()
    _run("openai:gpt-6-released-today", session)
    assert session.model_ref == "openai:gpt-6-released-today"


def test_switching_to_what_is_already_running_says_so_and_changes_nothing():
    session = _Session("openai:gpt-5.1")
    out = _run("openai:gpt-5.1", session)
    assert "already" in out.text.lower()
    assert session.model_ref == "openai:gpt-5.1"


def test_show_reports_without_opening_anything():
    out = _run("show", _Session())
    assert "deepseek:deepseek-v4-pro" in out.text
    assert "200,000" in out.text


# ── the picker ──────────────────────────────────────────────────────────────


def test_picking_applies_the_choice_to_the_session(catalogued):
    catalogued([ProviderCatalog("openai", (ModelInfo("openai", "gpt-5.1"),))],
               chooses="openai:gpt-5.1")
    session = _Session()
    out = _run("", session)
    assert session.model_ref == "openai:gpt-5.1"
    assert "→" in out.text


def test_cancelling_leaves_the_model_alone(catalogued):
    catalogued([ProviderCatalog("openai", (ModelInfo("openai", "gpt-5.1"),))],
               chooses=None)
    session = _Session()
    out = _run("", session)
    assert session.model_ref == "deepseek:deepseek-v4-pro"
    assert "still" in out.text.lower()


def test_when_every_provider_fails_the_reasons_are_shown_not_swallowed(catalogued):
    """"Still on the old model" is true and useless: the one thing worth
    reading is which key is wrong."""
    catalogued([ProviderCatalog("openai", (), error="AuthenticationError: 401"),
                ProviderCatalog("ollama", (), error="APIConnectionError")],
               chooses=None)
    session = _Session()
    out = _run("", session)
    assert "401" in out.text and "APIConnectionError" in out.text
    assert session.model_ref == "deepseek:deepseek-v4-pro"


def test_no_configured_provider_names_the_variable_to_set(catalogued):
    catalogued([], chooses=None)
    out = _run("", _Session())
    assert "ANTHROPIC_API_KEY" in out.text
    assert "OPENAI_API_KEY" in out.text


def test_refresh_drops_the_cache_and_re_reads_the_env_files(monkeypatch):
    """Whoever types refresh has usually just pasted a key into .env; making
    them restart the session to pick it up is the wrong answer."""
    seen = {"invalidated": False, "reloaded": False}

    async def _catalog(settings, refresh=False, timeout=8.0):
        return []

    monkeypatch.setattr("forge.model.catalog.catalog", _catalog)
    monkeypatch.setattr("forge.model.catalog.invalidate",
                        lambda: seen.__setitem__("invalidated", True))
    monkeypatch.setattr("forge.config.load_env_files",
                        lambda: seen.__setitem__("reloaded", True))
    _run("refresh", _Session())
    assert seen == {"invalidated": True, "reloaded": True}


# ── how a provider's answer is presented ────────────────────────────────────


def test_the_heading_carries_the_count_and_what_the_filter_hid():
    group = commands._provider_group(
        ProviderCatalog("openai", (ModelInfo("openai", "gpt-5.1"),), hidden=12), "")
    assert "openai" in group.title
    assert "1 models" in group.title
    assert "12 non-chat hidden" in group.title


def test_the_running_model_is_marked_in_its_group():
    group = commands._provider_group(
        ProviderCatalog("openai", (ModelInfo("openai", "gpt-5.1"),
                                   ModelInfo("openai", "gpt-5-mini"))),
        "openai:gpt-5.1")
    marked = [o for o in group.options if "current" in o.hint]
    assert [o.value for o in marked] == ["openai:gpt-5.1"]


def test_a_provider_that_failed_keeps_its_heading_and_shows_why():
    """Dropping the group would hide the one thing worth seeing — that the key
    is wrong."""
    group = commands._provider_group(
        ProviderCatalog("zai", (), error="AuthenticationError: 401"), "")
    assert group.options == ()
    assert "401" in group.note


def test_a_models_label_rides_along_as_the_hint():
    group = commands._provider_group(
        ProviderCatalog("anthropic",
                        (ModelInfo("anthropic", "claude-sonnet-4-6",
                                   label="Claude Sonnet 4.6"),)), "")
    assert group.options[0].hint == "Claude Sonnet 4.6"
    assert group.options[0].label == "claude-sonnet-4-6"
    assert group.options[0].value == "anthropic:claude-sonnet-4-6"
