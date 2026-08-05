"""The spinner and the status line.

A turn can run for minutes. Without a live line that is a blank terminal, and a
blank terminal is indistinguishable from a hang — which matters because the
operator's next move, wait or interrupt, depends entirely on telling those
apart.

The rules worth pinning down are the ones that are invisible when broken: the
spinner must never scroll (a terminal full of dead frames buries the
conversation), it must never raise into a turn, and the status line must not
print a number it did not measure.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forge.tui import ansi, status
from forge.tui.spinner import Spinner, _humanize_tokens


# ── Spinner rendering ───────────────────────────────────────────────────────


def test_the_line_answers_alive_how_long_and_what_it_costs():
    sp = Spinner()
    sp.add_chars(8_000)          # ~2k tokens
    line = sp.render(now=32.0)

    assert "32s" in line
    assert "2.0k tokens" in line
    assert "interrupt" in line


def test_the_verb_rotates_so_a_long_turn_does_not_read_as_frozen():
    sp = Spinner()
    early = sp.render(now=1.0)
    later = sp.render(now=1.0 + 3 * 6.0)
    assert early != later


def test_a_harness_status_replaces_the_generic_verb():
    """What it is actually doing beats a rotating adjective every time."""
    sp = Spinner()
    sp.set_status("Running run_command")
    assert "Running run_command" in sp.render(now=1.0)


def test_a_trivial_token_count_is_not_shown():
    """Below a few hundred it is noise, and noise trains the eye to skip the line."""
    sp = Spinner()
    sp.add_chars(40)
    assert "tokens" not in sp.render(now=1.0)


@pytest.mark.parametrize("n,expected", [
    (999, "999"), (1_500, "1.5k"), (2_400_000, "2.4m"),
])
def test_token_counts_are_humanized(n, expected):
    assert _humanize_tokens(n) == expected


# ── Spinner behaviour ───────────────────────────────────────────────────────


def test_it_never_scrolls():
    """Every frame overwrites the last. A transcript of dead spinner frames is
    worse than no spinner."""
    written: list[str] = []
    real = ansi.write

    def _capture(text="", end="\n"):
        written.append(text + end)

    async def scenario():
        ansi.write = _capture
        try:
            sp = Spinner()
            sp.start()
            await asyncio.sleep(0.4)
            await sp.stop()
        finally:
            ansi.write = real

    asyncio.run(scenario())
    if ansi._ENABLED:  # noqa: SLF001 — the transient path is a no-op when off
        assert written, "the spinner drew nothing"
        assert not any(chunk.endswith("\n") and chunk.strip() for chunk in written), \
            "the spinner emitted a newline and will scroll"


def test_stopping_twice_is_harmless():
    async def scenario():
        sp = Spinner()
        sp.start()
        await sp.stop()
        await sp.stop()

    asyncio.run(scenario())


def test_stopping_one_that_never_started_is_harmless():
    asyncio.run(Spinner().stop())


def test_clear_before_start_does_not_touch_the_terminal():
    Spinner().clear()


# ── Status line ─────────────────────────────────────────────────────────────


class _Ledger:
    def __init__(self, turns=0, fullness=0.0, inp=0, out=0):
        self.turns, self.input_tokens, self.output_tokens = turns, inp, out
        self._fullness = fullness

    @property
    def fullness(self):
        return self._fullness


class _Cfg:
    agent_id = "optimus"
    permission_mode = "act"


class _Session:
    def __init__(self, tmp_path, ledger=None, mode="act"):
        self.cfg = _Cfg()
        self.model_ref = "deepseek:deepseek-v4-pro"
        self.workspace = tmp_path
        self.ledger = ledger or _Ledger()
        self._mode = mode

    @property
    def permission_mode(self):
        return self._mode


def test_a_fresh_session_shows_who_and_where(tmp_path):
    parts = status.segments(_Session(tmp_path))
    assert parts[0] == "optimus"
    assert "deepseek-v4-pro" in parts[1]      # provider prefix is implied
    assert parts[-1] == tmp_path.name


def test_usage_appears_once_there_is_usage(tmp_path):
    session = _Session(tmp_path, _Ledger(turns=3, fullness=0.18, inp=12_400, out=3_100))
    joined = " ".join(status.segments(session))

    assert "18% ctx" in joined
    assert "12.4k in / 3.1k out" in joined


def test_no_dollar_figure_without_configured_prices(tmp_path, monkeypatch):
    """A rate table baked into the source is a guess wearing a measurement's
    clothes, and it goes stale silently."""
    monkeypatch.setattr(status, "PRICES", {})
    session = _Session(tmp_path, _Ledger(turns=1, inp=1_000_000, out=1_000_000))
    assert "$" not in " ".join(status.segments(session))


def test_a_configured_price_produces_an_approximate_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "PRICES", {"deepseek-v4-pro": (1.0, 2.0)})
    session = _Session(tmp_path, _Ledger(turns=1, inp=1_000_000, out=1_000_000))

    joined = " ".join(status.segments(session))
    assert "~$3.00" in joined      # 1.00 in + 2.00 out, and marked approximate


def test_the_default_mode_is_not_shown_but_plan_is(tmp_path):
    """A line that always says 'act' trains the eye to skip it."""
    assert "act" not in status.segments(_Session(tmp_path, mode="act"))
    assert "plan" in status.segments(_Session(tmp_path, mode="plan"))


def test_the_line_is_trimmed_to_the_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(ansi, "terminal_width", lambda default=80: 30)
    session = _Session(tmp_path, _Ledger(turns=9, fullness=0.9, inp=999_000, out=999_000))

    line = status.render(session)
    assert len(line) <= 40   # allows for style codes around a <30-char payload


def test_status_is_suppressible(tmp_path, monkeypatch, capsys):
    """Piping a session into a file should produce a transcript."""
    monkeypatch.setenv("FORGE_NO_STATUS", "1")
    status.write(_Session(tmp_path))
    assert capsys.readouterr().out == ""


# ── git branch ──────────────────────────────────────────────────────────────


def test_a_non_repository_has_no_branch(tmp_path):
    status.forget_branch(tmp_path)
    assert status.git_branch(tmp_path) == ""


def test_the_branch_is_cached_then_forgettable(tmp_path, monkeypatch):
    """Runs before every prompt; a subprocess per pause is a real cost."""
    calls = {"n": 0}
    real = status.subprocess.run

    def _counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(status.subprocess, "run", _counted)
    status.forget_branch(tmp_path)
    status.git_branch(tmp_path)
    status.git_branch(tmp_path)
    assert calls["n"] == 1

    status.forget_branch(tmp_path)
    status.git_branch(tmp_path)
    assert calls["n"] == 2
