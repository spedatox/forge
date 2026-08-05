"""Sessions that survive the terminal closing.

Before this, a conversation lived in memory and nowhere else: closing the
window during a long refactor lost not the files, which are on disk, but
everything about why they look like that — what was tried, what was rejected,
what the plan was.

The rules under test are the ones that are silent when broken: a save must
never take down the turn that produced it, a partially written file must never
become an unreadable session, and a resumed conversation must not carry
read-tracking that would let the model edit from remembered text.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from forge.tui import persistence
from forge.tui.commands import resolve as resolve_command


class _Cfg:
    agent_id = "optimus"
    permission_mode = "act"


class _Session:
    def __init__(self, tmp_path, messages=None, turns=0, session_id="s1"):
        self.cfg = _Cfg()
        self.model_ref = "deepseek:deepseek-v4-pro"
        self.workspace = tmp_path
        self.messages = messages or []
        self.turns = turns
        self.session_id = session_id
        self.last_truncated = ("grep", "old output")

    @property
    def permission_mode(self):
        return "act"


def _run(name, args, session):
    cmd, _ = resolve_command(f"/{name}")
    return asyncio.run(cmd.run(args, session))


CONVERSATION = [
    {"role": "user", "content": "fix the retry logic in client.py"},
    {"role": "assistant", "content": "Done."},
]


# ── Saving ──────────────────────────────────────────────────────────────────


def test_a_session_round_trips(tmp_path):
    session = _Session(tmp_path, CONVERSATION, turns=2)
    assert persistence.save(session, "s1") is not None

    assert persistence.load(tmp_path, "s1") == CONVERSATION


def test_the_title_is_the_operators_own_words(tmp_path):
    """Not a generated summary: naming a session costs a model call, and a
    wrong title is worse than a blunt one when the job is recognising it."""
    persistence.save(_Session(tmp_path, CONVERSATION), "s1")
    assert persistence.listing(tmp_path)[0].title.startswith("fix the retry logic")


def test_a_long_first_prompt_is_shortened(tmp_path):
    long = [{"role": "user", "content": "x" * 500}]
    persistence.save(_Session(tmp_path, long), "s1")
    assert len(persistence.listing(tmp_path)[0].title) < 100


def test_the_title_reads_through_content_blocks(tmp_path):
    blocks = [{"role": "user", "content": [{"type": "text", "text": "block prompt"}]}]
    persistence.save(_Session(tmp_path, blocks), "s1")
    assert "block prompt" in persistence.listing(tmp_path)[0].title


def test_sessions_are_scoped_to_the_workspace(tmp_path):
    """A conversation about one repository is noise in another, and a shared
    store would offer other projects' work to resume."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    persistence.save(_Session(a, CONVERSATION), "s1")

    assert persistence.listing(a)
    assert persistence.listing(b) == []


def test_a_save_never_raises_into_the_turn(tmp_path, monkeypatch):
    """Losing a save costs a resume. Raising here would cost the turn that was
    just completed."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert persistence.save(_Session(tmp_path, CONVERSATION), "s1") is None


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "s1")
    assert not list(persistence.sessions_dir(tmp_path).glob("*.tmp"))


def test_old_sessions_are_pruned(tmp_path):
    for i in range(persistence.KEEP_SESSIONS + 5):
        persistence.save(_Session(tmp_path, CONVERSATION), f"s{i:03d}")
    kept = list(persistence.sessions_dir(tmp_path).glob("*.json"))
    assert len(kept) <= persistence.KEEP_SESSIONS


# ── Reading ─────────────────────────────────────────────────────────────────


def test_an_unreadable_file_is_skipped_not_fatal(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "good")
    (persistence.sessions_dir(tmp_path) / "bad.json").write_text("{not json",
                                                                 encoding="utf-8")
    entries = persistence.listing(tmp_path)
    assert [e.id for e in entries] == ["good"]


def test_a_future_format_is_ignored_rather_than_misread(tmp_path):
    directory = persistence.sessions_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "future.json").write_text(
        json.dumps({"version": 999, "messages": []}), encoding="utf-8")

    assert persistence.listing(tmp_path) == []
    assert persistence.load(tmp_path, "future") is None


def test_loading_something_absent_returns_none(tmp_path):
    assert persistence.load(tmp_path, "nope") is None


def test_newest_first(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "old")
    time.sleep(0.01)
    persistence.save(_Session(tmp_path, CONVERSATION), "new")
    assert persistence.listing(tmp_path)[0].id == "new"


@pytest.mark.parametrize("reference,expected", [
    ("1", "new"), ("2", "old"), ("old", "old"), ("", "new"),
])
def test_a_session_resolves_by_position_or_id(tmp_path, reference, expected):
    """Position is what was just read off the screen; the id is what survives
    running the list again."""
    persistence.save(_Session(tmp_path, CONVERSATION), "old")
    time.sleep(0.01)
    persistence.save(_Session(tmp_path, CONVERSATION), "new")

    assert persistence.resolve(tmp_path, reference).id == expected


@pytest.mark.parametrize("reference", ["99", "nonexistent"])
def test_an_unmatched_reference_resolves_to_nothing(tmp_path, reference):
    persistence.save(_Session(tmp_path, CONVERSATION), "only")
    assert persistence.resolve(tmp_path, reference) is None


# ── The commands ────────────────────────────────────────────────────────────


def test_sessions_lists_with_positions(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION, turns=3), "s1")
    text = _run("sessions", "", _Session(tmp_path, session_id="other")).text

    assert "1." in text and "fix the retry logic" in text
    assert "3 turns" in text


def test_sessions_marks_the_current_one(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "s1")
    text = _run("sessions", "", _Session(tmp_path, session_id="s1")).text
    assert "(this one)" in text


def test_sessions_with_none_says_where_they_go(tmp_path):
    assert ".forge/sessions" in _run("sessions", "", _Session(tmp_path)).text


def test_resume_replaces_the_transcript_and_adopts_the_id(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION, turns=2), "earlier")
    live = _Session(tmp_path, [{"role": "user", "content": "different"}],
                    session_id="current")

    out = _run("resume", "earlier", live)

    assert live.messages == CONVERSATION
    assert live.session_id == "earlier", "continuing must update the same record"
    assert live.turns == 2
    assert "Resumed" in out.text


def test_resume_drops_stale_expansion_state(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "earlier")
    live = _Session(tmp_path, session_id="current")
    live.last_truncated = ("grep", "output from a different conversation")

    _run("resume", "earlier", live)
    assert live.last_truncated is None


def test_resume_warns_that_files_may_have_moved_on(tmp_path):
    """A conversation from last week describes files as they were."""
    persistence.save(_Session(tmp_path, CONVERSATION), "earlier")
    out = _run("resume", "earlier", _Session(tmp_path, session_id="current"))
    assert "re-read before editing" in out.text


def test_resuming_the_current_session_is_refused(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "s1")
    out = _run("resume", "s1", _Session(tmp_path, session_id="s1"))
    assert "already in" in out.text


def test_resume_with_no_saved_sessions(tmp_path):
    assert "Nothing to resume" in _run("resume", "", _Session(tmp_path)).text


def test_resume_of_an_unknown_reference_points_at_the_list(tmp_path):
    persistence.save(_Session(tmp_path, CONVERSATION), "s1")
    out = _run("resume", "zzz", _Session(tmp_path, session_id="cur"))
    assert "/sessions" in out.text
