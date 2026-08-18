"""The grouped picker.

Everything here is about the list staying usable when it is long: headings are
skipped by the arrows rather than landed on, typing narrows instead of
scrolling, a keystroke that would empty the screen is refused, and the window
follows the cursor rather than the cursor following the screen.

Driven by a scripted key sequence, so none of it needs a terminal.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tui import keys, picker
from forge.tui.picker import Group, Option


def _groups():
    return [
        Group("anthropic", (Option("anthropic:claude-sonnet-4-6", "claude-sonnet-4-6"),
                            Option("anthropic:claude-haiku-4-5", "claude-haiku-4-5"))),
        Group("openai", (Option("openai:gpt-5.1", "gpt-5.1"),)),
        Group("ollama", (), note="ConnectError: connection refused"),
    ]


@pytest.fixture
def scripted(monkeypatch):
    """Feed `_select_sync` a key sequence and silence the repaints."""
    def _install(sequence):
        pending = list(sequence)

        def _read(timeout=None):
            return pending.pop(0) if pending else None
        monkeypatch.setattr(keys, "read_key_raw", _read)
        monkeypatch.setattr(picker.ansi, "repaint", lambda lines, rows: len(lines))
        monkeypatch.setattr(picker.ansi, "terminal_height", lambda default=24: 24)
    return _install


def test_headings_and_notes_are_rows_but_never_selectable():
    rows = picker._rows(_groups(), "")
    assert [r.text for r in rows if r.option is None] == [
        "anthropic", "openai", "ollama", "  ConnectError: connection refused"]
    assert len([r for r in rows if r.option]) == 3


def test_filtering_drops_the_groups_that_have_nothing_left():
    rows = picker._rows(_groups(), "gpt")
    assert [r.text for r in rows] == ["openai", "gpt-5.1"]


def test_a_multi_word_filter_matches_in_any_order():
    rows = picker._rows(_groups(), "4-6 claude")
    assert [r.text for r in rows if r.option] == ["claude-sonnet-4-6"]


def test_the_arrows_step_over_headings(scripted):
    scripted([keys.DOWN, keys.ENTER])
    # Starts on the first option (sonnet); one DOWN must land on haiku, not on
    # the "openai" heading between them.
    assert picker._select_sync(_groups(), "model", "") == "anthropic:claude-haiku-4-5"


def test_down_from_the_last_option_wraps_to_the_first(scripted):
    scripted([keys.DOWN, keys.DOWN, keys.DOWN, keys.ENTER])
    assert picker._select_sync(_groups(), "model", "") == "anthropic:claude-sonnet-4-6"


def test_the_cursor_starts_on_what_is_already_selected(scripted):
    scripted([keys.ENTER])
    assert picker._select_sync(_groups(), "model", "openai:gpt-5.1") == "openai:gpt-5.1"


def test_typing_narrows_and_enter_takes_the_survivor(scripted):
    scripted(["h", "a", "i", keys.ENTER])
    assert picker._select_sync(_groups(), "model", "") == "anthropic:claude-haiku-4-5"


def test_a_keystroke_that_would_empty_the_list_is_refused(scripted):
    """Accepting it leaves a screen with nothing on it and no cursor to move —
    an operator's only working key would be backspace, on what looks like a
    crash."""
    scripted(["z", "q", "x", keys.ENTER])
    assert picker._select_sync(_groups(), "model", "") == "anthropic:claude-sonnet-4-6"


def test_backspace_widens_the_filter_again(scripted):
    scripted(["g", "p", "t", "\x7f", "\x7f", "\x7f", keys.DOWN, keys.ENTER])
    assert picker._select_sync(_groups(), "model", "") == "anthropic:claude-haiku-4-5"


def test_escape_cancels_and_chooses_nothing(scripted):
    scripted([keys.CANCEL])
    assert picker._select_sync(_groups(), "model", "") is None


def test_a_console_that_stops_answering_cancels_rather_than_hangs(scripted):
    scripted([])                       # read_key_raw returns None immediately
    assert picker._select_sync(_groups(), "model", "") is None


def test_the_window_keeps_the_cursor_inside_it():
    rows = [picker._Row(text=str(n), option=Option(str(n))) for n in range(40)]
    assert picker._window(rows, 0, 10) == (0, 10)
    start, end = picker._window(rows, 30, 10)
    assert start <= 30 < end
    # The last row must be reachable without the window running past the list.
    assert picker._window(rows, 39, 10) == (30, 40)


def test_a_short_list_is_not_windowed():
    rows = [picker._Row(text="x", option=Option("x"))]
    assert picker._window(rows, 0, 10) == (0, 1)


def test_without_a_tty_it_numbers_the_options_and_reads_a_line(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(picker.ansi, "write", lambda text="", end="\n": written.append(text))
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    assert picker._typed_sync(_groups(), "model") == "openai:gpt-5.1"
    assert any("connection refused" in line for line in written)   # the note survives


def test_the_typed_fallback_also_takes_the_name(monkeypatch):
    monkeypatch.setattr(picker.ansi, "write", lambda text="", end="\n": None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "openai:gpt-5.1")
    assert picker._typed_sync(_groups(), "") == "openai:gpt-5.1"


def test_the_typed_fallback_takes_an_unambiguous_partial(monkeypatch):
    monkeypatch.setattr(picker.ansi, "write", lambda text="", end="\n": None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "haiku")
    assert picker._typed_sync(_groups(), "") == "anthropic:claude-haiku-4-5"


def test_an_ambiguous_partial_picks_nothing_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(picker.ansi, "write", lambda text="", end="\n": None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "claude")
    assert picker._typed_sync(_groups(), "") is None


def test_pick_returns_none_when_there_is_nothing_to_choose_between():
    groups = [Group("ollama", (), note="connection refused")]
    assert asyncio.run(picker.pick(groups)) is None


def test_a_terminal_that_cannot_repaint_gets_the_typed_list(monkeypatch):
    """Arrows with no frame under them is the one degradation worse than the
    numbered fallback: the operator presses keys at a blank screen."""
    monkeypatch.setattr(picker.keys, "available", lambda: True)
    monkeypatch.setattr(picker.ansi, "styled", lambda: False)
    monkeypatch.setattr(picker.ansi, "write", lambda text="", end="\n": None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    assert asyncio.run(picker.pick(_groups())) == "anthropic:claude-sonnet-4-6"
