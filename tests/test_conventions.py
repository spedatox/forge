"""Repository conventions reaching the system prompt.

An agent that has not read a project's conventions rediscovers them by being
corrected — wrong test runner, wrong docstring style, a dependency the project
deliberately avoids. Each costs a turn to hit and another to fix, in every
session, forever.

`PromptFragment` documented `"repo:CLAUDE.md"` as a source from the beginning
and nothing ever produced one. These tests cover the half that was missing, and
the labelling that makes it usable: an agent handed both its own instructions
and a repository's has to be able to tell which is which to resolve a conflict.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.agents import conventions
from forge.agents.prompt import PromptFragment, compose_system_prompt
from forge.tui.commands import resolve as resolve_command


class _Cfg:
    agent_id = "optimus"
    permission_mode = "act"
    system_prompt = "You are Optimus."


class _Session:
    def __init__(self, tmp_path):
        self.cfg = _Cfg()
        self.workspace = tmp_path
        self.model_ref = "deepseek:deepseek-v4-pro"
        self.messages = []
        self.turns = 0
        self.cell = None


def _run(name, args, session):
    cmd, _ = resolve_command(f"/{name}")
    return asyncio.run(cmd.run(args, session))


# ── Finding the file ────────────────────────────────────────────────────────


def test_nothing_to_load_in_a_bare_directory(tmp_path):
    assert conventions.find(tmp_path) is None
    assert conventions.load(tmp_path) is None
    assert conventions.fragment(tmp_path) is None


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_either_convention_filename_is_read(tmp_path, name):
    """CLAUDE.md is supported because many repositories already have one, and
    asking for a duplicate would be asking for two copies to maintain."""
    (tmp_path / name).write_text("Use pytest, never unittest.", encoding="utf-8")

    source, text = conventions.load(tmp_path)
    assert "pytest" in text
    assert source == f"repo:{name}"


def test_agents_md_wins_over_claude_md(tmp_path):
    """The vendor-neutral name is the one a repository should be able to
    prefer once it has both."""
    (tmp_path / "AGENTS.md").write_text("agents file", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude file", encoding="utf-8")

    assert "agents file" in conventions.load(tmp_path)[1]


def test_an_empty_file_is_not_a_fragment(tmp_path):
    (tmp_path / "AGENTS.md").write_text("   \n\n", encoding="utf-8")
    assert conventions.load(tmp_path) is None


def test_an_oversized_file_is_truncated_and_says_why(tmp_path):
    """It is paid for on every turn, so the operator should hear about it."""
    (tmp_path / "AGENTS.md").write_text("x" * (conventions.MAX_CHARS + 5_000),
                                        encoding="utf-8")

    _, text = conventions.load(tmp_path)
    assert len(text) < conventions.MAX_CHARS + 500
    assert "every turn" in text


def test_an_unreadable_file_costs_its_contents_not_the_session(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("something", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(type(tmp_path / "AGENTS.md"), "read_text", _boom)
    assert conventions.load(tmp_path) is None


# ── Reaching the prompt, labelled ───────────────────────────────────────────


def test_the_fragment_is_labelled_with_its_filename(tmp_path):
    """Folded into the profile it would be indistinguishable from the agent's
    own instructions, and a conflict between them unresolvable."""
    (tmp_path / "AGENTS.md").write_text("Never add dependencies.", encoding="utf-8")

    prompt = compose_system_prompt([
        PromptFragment("profile", "You are Optimus."),
        conventions.fragment(tmp_path),
    ])

    assert "You are Optimus." in prompt
    assert "Never add dependencies." in prompt
    assert "AGENTS.MD" in prompt        # the composer upper-cases the label


def test_the_profile_stays_unlabelled(tmp_path):
    """It is the agent speaking as itself."""
    prompt = compose_system_prompt([PromptFragment("profile", "You are Optimus.")])
    assert prompt.strip() == "You are Optimus."


# ── /init ───────────────────────────────────────────────────────────────────


def test_init_asks_the_agent_to_survey_first(tmp_path):
    """A generic conventions file is worse than none — it occupies the same
    space on every turn while saying nothing."""
    out = _run("init", "", _Session(tmp_path))

    assert out.prompt
    assert "AGENTS.md" in out.prompt
    assert "do not guess" in out.prompt.lower()
    assert "verified" in out.prompt


def test_init_will_not_silently_overwrite(tmp_path):
    (tmp_path / "AGENTS.md").write_text("hand written", encoding="utf-8")

    out = _run("init", "", _Session(tmp_path))

    assert not out.prompt, "an existing file was about to be rewritten"
    assert "already exists" in out.text
    assert "force" in out.text


def test_init_force_overrides(tmp_path):
    (tmp_path / "AGENTS.md").write_text("hand written", encoding="utf-8")
    assert _run("init", "force", _Session(tmp_path)).prompt


def test_init_recognises_an_existing_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("existing", encoding="utf-8")
    out = _run("init", "", _Session(tmp_path))
    assert "CLAUDE.md" in out.text and not out.prompt
