"""Reading something longer than the screen without losing the screen.

`/transcript` dumped the whole conversation into scrollback, which on a long
session buries the conversation under a copy of itself — and takes the
operator's own scrollback, the thing they would have scrolled to find it, with
it. Codex answers this with a viewport overlay; Forge's TUI is inline and owns
no viewport, so it answers with the shape `less` has used for forty years.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tui import ansi, keys, pager


def _out(monkeypatch) -> list[str]:
    written: list[str] = []
    monkeypatch.setattr(ansi, "write", lambda t="", end="\n": written.append(t))
    monkeypatch.setattr(ansi, "rewind", lambda n: True)
    return written


def test_short_text_is_printed_whole(monkeypatch):
    """Paging something that already fits is an extra keystroke to see what was
    already visible."""
    written = _out(monkeypatch)
    monkeypatch.setattr(ansi, "terminal_height", lambda default=24: 40)
    monkeypatch.setattr(keys, "available", lambda: True)

    asyncio.run(pager.page("one\ntwo\nthree"))
    assert written == ["one\ntwo\nthree"]


def test_a_terminal_that_cannot_read_keys_gets_the_old_behaviour(monkeypatch):
    """Degrades to exactly what /transcript did before, so nothing that worked
    stops working."""
    written = _out(monkeypatch)
    monkeypatch.setattr(ansi, "terminal_height", lambda default=24: 10)
    monkeypatch.setattr(keys, "available", lambda: False)

    body = "\n".join(str(i) for i in range(200))
    asyncio.run(pager.page(body))
    assert written == [body]


def test_q_stops_and_says_how_much_was_left(monkeypatch):
    """Quitting early is the common case and the point — it is what keeps the
    operator's scrollback theirs."""
    written = _out(monkeypatch)
    monkeypatch.setattr(ansi, "terminal_height", lambda default=24: 12)
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(keys, "read_key", lambda timeout=None: "q")

    asyncio.run(pager.page("\n".join(str(i) for i in range(100))))
    joined = "\n".join(written)
    assert "unread" in joined
    assert "99" not in joined, "it kept printing after being told to stop"


def test_space_walks_to_the_end(monkeypatch):
    written = _out(monkeypatch)
    monkeypatch.setattr(ansi, "terminal_height", lambda default=24: 12)
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(keys, "read_key", lambda timeout=None: " ")

    asyncio.run(pager.page("\n".join(str(i) for i in range(100))))
    assert "99" in "\n".join(written)


def test_ctrl_c_stops_the_pager(monkeypatch):
    written = _out(monkeypatch)
    monkeypatch.setattr(ansi, "terminal_height", lambda default=24: 12)
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(keys, "read_key", lambda timeout=None: keys.CANCEL)

    asyncio.run(pager.page("\n".join(str(i) for i in range(100))))
    assert "unread" in "\n".join(written)


def test_a_terminal_that_stops_answering_gets_the_rest(monkeypatch):
    """None means the terminal will never deliver another key. Leaving the
    operator on a half-shown page with no way forward is the one outcome worse
    than dumping it."""
    written = _out(monkeypatch)
    monkeypatch.setattr(ansi, "terminal_height", lambda default=24: 12)
    monkeypatch.setattr(keys, "available", lambda: True)
    monkeypatch.setattr(keys, "read_key", lambda timeout=None: None)

    asyncio.run(pager.page("\n".join(str(i) for i in range(100))))
    assert "99" in "\n".join(written)
