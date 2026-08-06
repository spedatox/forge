"""The command line, as it is actually typed.

Two ergonomics that only matter once Forge is installed globally rather than
run out of its own checkout, and both fail in ways that look like a broken
install rather than a missing convenience.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.__main__ import _COMMANDS, _with_default_command
from forge.config import load_dotenv


# ── Bare `forge` opens a session here ────────────────────────────────────────


def test_bare_forge_starts_a_session():
    """The overwhelmingly common invocation is 'work with me in this repo'.
    Requiring a subcommand for it makes the tool feel like infrastructure."""
    assert _with_default_command([]) == ["chat"]


@pytest.mark.parametrize("flags", [["-v"], ["--model", "x"], ["--cwd", "/tmp"]])
def test_flags_pass_through_to_the_session(flags):
    assert _with_default_command(flags) == ["chat", *flags]


@pytest.mark.parametrize("cmd", _COMMANDS)
def test_an_explicit_subcommand_is_untouched(cmd):
    """Nothing that worked before this changes."""
    assert _with_default_command([cmd]) == [cmd]


def test_a_subcommands_own_arguments_survive():
    assert _with_default_command(["connect", "--agent", "optimus"]) == \
        ["connect", "--agent", "optimus"]


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_still_reaches_the_top_level_parser(flag):
    """Otherwise `forge --help` would silently document only `chat`."""
    assert _with_default_command([flag]) == [flag]


# ── Credentials for a global install ─────────────────────────────────────────


def test_a_real_environment_variable_always_wins(tmp_path, monkeypatch):
    """An operator who exported a key for one command must not have it
    replaced by a stale line in a file they forgot about."""
    env = tmp_path / ".env"
    env.write_text("FORGE_TEST_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("FORGE_TEST_TOKEN", "from-shell")

    load_dotenv(env)
    assert os.environ["FORGE_TEST_TOKEN"] == "from-shell"


def test_the_project_env_wins_over_the_user_one(tmp_path, monkeypatch):
    """The precedence the global install depends on: most specific first, and
    load_dotenv never overwrites a name that is already set. A project pinning
    its own model or backend must not be overridden by the user's defaults."""
    monkeypatch.delenv("FORGE_TEST_TOKEN", raising=False)
    project = tmp_path / "project.env"
    user = tmp_path / "user.env"
    project.write_text("FORGE_TEST_TOKEN=project\n", encoding="utf-8")
    user.write_text("FORGE_TEST_TOKEN=user\nFORGE_TEST_ONLY_USER=yes\n", encoding="utf-8")

    load_dotenv(project)          # the call order in __main__
    load_dotenv(user)

    assert os.environ["FORGE_TEST_TOKEN"] == "project"
    # ...and the user file still supplies what the project did not mention,
    # which is what makes `forge` work in a repo that never heard of it.
    assert os.environ["FORGE_TEST_ONLY_USER"] == "yes"


def test_a_missing_user_env_is_not_an_error():
    """A fresh machine has no ~/.forge/.env, and that must not stop the CLI."""
    assert load_dotenv(Path("nope") / "definitely" / "absent.env") == 0
