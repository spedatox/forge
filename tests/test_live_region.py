"""The multi-row live region.

`Spinner` owns one line and is tested in test_live_line.py. This is what sits
around it once the loop has concurrency to show — up to ten parallel tools and
four subagents, none of which was visible before.

The invariant that carries everything else: the region never scrolls. It draws N
rows and erases exactly N, so a five-minute turn leaves nothing behind. Get that
wrong by one row per frame and the conversation is buried under dead frames.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from forge.tui import ansi, ui
from forge.tui.live import MAX_ROWS, LiveRegion, Row


def _region(**rows: float) -> LiveRegion:
    """A region with tools started `n` seconds ago, keyed by name."""
    r = LiveRegion()
    r.spinner.start_clock()
    now = time.monotonic()
    for name, ago in rows.items():
        r.tool_started(name, name.capitalize(), f"{name}.py")
        r._rows[name].started = now - ago
    return r


# ── The no-scroll invariant ──────────────────────────────────────────────────

def test_clear_erases_exactly_what_was_drawn():
    """The pairing IS the invariant. A rewind short by one leaves a dead row per
    frame; long by one eats the line above the region, which is somebody's
    transcript."""
    rewound: list[int] = []
    real_write, real_rewind = ansi.write, ansi.rewind
    ansi.write = lambda text="", end="\n": None
    ansi.rewind = lambda n: (rewound.append(n), True)[1]
    try:
        with _styled():
            r = _region(alpha=1.0, beta=2.0)
            r._draw()
            drawn = r._drawn
            r.clear()
    finally:
        ansi.write, ansi.rewind = real_write, real_rewind

    assert drawn == 3, "header plus two rows"
    assert rewound == [3], "cleared exactly the rows it drew"


# ── Drawing only what can be taken back ──────────────────────────────────────

@contextlib.contextmanager
def _styled(width: int = 200):
    """A terminal that takes escape sequences, which the test process is not.

    Every assertion about reclaiming rows is vacuous without this: `rewind` is a
    no-op on an unstyled stream, so a region that drew anyway would look correct
    to a test that stubbed `rewind` to succeed. That is exactly how the flood
    below went unnoticed."""
    was = ansi._ENABLED                       # noqa: SLF001
    ansi._ENABLED = True                      # noqa: SLF001
    real = ansi.terminal_width
    ansi.terminal_width = lambda default=80: width
    try:
        yield
    finally:
        ansi._ENABLED = was                   # noqa: SLF001
        ansi.terminal_width = real


def test_it_draws_nothing_where_it_cannot_rewind():
    """NO_COLOR, a dumb TERM, a pipe, a Windows console that refused VT mode.

    `Spinner` is silent in all four because `transient` no-ops. This drew with
    `write` and reclaimed with `rewind`, so every tick was committed and none
    taken back: the region flowed down the screen a frame at a time instead of
    updating in place."""
    out: list[str] = []
    real = ansi.write
    ansi.write = lambda text="", end="\n": out.append(text + end)
    try:
        assert ansi.styled() is False, "the test process has no tty"
        r = _region(alpha=1.0)
        r._draw()
        r._draw()
        r._draw()
    finally:
        ansi.write = real
    assert out == [], "an unrewindable terminal gets silence, not a frame per tick"
    assert r._drawn == 0


def test_a_wrapped_row_is_measured_in_screen_rows():
    """`len(lines)` is not the height of the region.

    A row wider than the terminal occupies two rows and is reclaimed as one, so
    the frame creeps down the screen leaving a line of debris per tick — the
    "it updates in place but keeps blinking" shape."""
    with _styled(width=40):
        r = LiveRegion()
        r.spinner.start_clock()
        r.tool_started("1", "Run", "x" * 120)      # far past 40 columns
        out: list[str] = []
        real = ansi.write
        ansi.write = lambda text="", end="\n": out.append(text + end)
        try:
            r._draw()
        finally:
            ansi.write = real
    lines = r.render()
    expected = sum(ansi.wrapped_height(ln, 40) for ln in lines)
    assert r._drawn == expected
    assert r._drawn > len(lines), "the long row wrapped; the count has to know"


def test_a_frame_is_drawn_in_one_write():
    """Flicker is not a rendering detail, it is the blank gap between an erase
    and the redraw that follows it. One write, and there is no gap to see."""
    with _styled():
        r = _region(alpha=1.0, beta=2.0)
        writes: list[str] = []
        real = ansi.write
        ansi.write = lambda text="", end="\n": writes.append(text + end)
        try:
            r._draw()
            r._draw()
        finally:
            ansi.write = real
    assert len(writes) == 2, "one write per frame, not one per row plus a clear"
    assert "\x1b[J" not in writes[0].rsplit("\x1b[J", 1)[0], "erase only at the end"
    assert writes[1].startswith("\r\x1b["), "the second frame moves up, it does not blank"


def test_clear_is_harmless_before_anything_is_drawn():
    LiveRegion().clear()


def test_a_frame_is_erased_before_the_next_is_drawn():
    """Over many ticks the net committed rows must stay at zero."""
    net = 0
    real_write, real_rewind = ansi.write, ansi.rewind

    def _r(n):
        nonlocal net
        net -= n
        return True

    def _w(text="", end="\n"):
        """Counts what the frame commits AND what its cursor-up takes back —
        `repaint` writes both in one payload, so the tally has to read it."""
        nonlocal net
        net += (text + end).count("\n")
        for move in ansi._ESCAPE_UP.findall(text):       # noqa: SLF001
            net -= int(move or 1)

    async def scenario():
        ansi.write, ansi.rewind = _w, _r
        try:
            with _styled():
                r = LiveRegion()
                r.tool_started("1", "Grep", "x")
                r.start()
                await asyncio.sleep(0.5)
                await r.stop()
        finally:
            ansi.write, ansi.rewind = real_write, real_rewind

    asyncio.run(scenario())
    assert net == 0, f"the region leaked {net} rows into scrollback"


# ── What is in flight ────────────────────────────────────────────────────────

def test_a_started_tool_gets_a_row():
    r = _region(alpha=1.0)
    assert len(r.rows()) == 1


def test_a_finished_tool_leaves_the_region():
    """Finished calls are already committed to scrollback by the renderer.
    Keeping them here would print every call in a batch twice."""
    r = _region(alpha=1.0, beta=1.0)
    r.tool_finished("alpha")
    assert len(r.rows()) == 1


def test_longest_running_leads():
    """With ten in flight the only question worth asking at a glance is which
    one is not coming back."""
    r = _region(quick=1.0, slow=90.0, middling=20.0)
    first = ansi._ESCAPE.sub("", r.rows()[0])  # noqa: SLF001
    assert "Slow" in first


def test_the_region_is_capped_and_says_so():
    r = _region(**{f"t{i}": float(i) for i in range(1, 13)})
    lines = r.rows()
    assert len(lines) == MAX_ROWS + 1, "the cap, plus the overflow note"
    assert "+4 more running" in ansi._ESCAPE.sub("", lines[-1])  # noqa: SLF001


def test_the_closing_connector_is_only_ever_last():
    """A `└─` with a row under it is worse than no tree at all."""
    r = _region(**{f"t{i}": float(i) for i in range(1, 13)})
    plain = [ansi._ESCAPE.sub("", ln) for ln in r.rows()]  # noqa: SLF001
    assert sum(1 for ln in plain if "└─" in ln) == 1
    assert "└─" in plain[-1], "the overflow note closes the tree, not the last tool"


def test_a_single_row_closes_the_tree():
    r = _region(only=1.0)
    assert "└─" in ansi._ESCAPE.sub("", r.rows()[0])  # noqa: SLF001


# ── Subagents, which were previously invisible ───────────────────────────────

def test_a_subagent_gets_a_row_and_a_distinct_glyph():
    r = LiveRegion()
    r.subagent_started("s1", "verify", "check the guard")
    line = ansi._ESCAPE.sub("", r.rows()[0])  # noqa: SLF001
    assert "verify" in line
    assert "◆" in line, "a child loop is not a tool call"


def test_a_subagents_phase_updates_in_place():
    r = LiveRegion()
    r.subagent_started("s1", "verify")
    r.subagent_phase("s1", "Run")
    assert "Run" in ansi._ESCAPE.sub("", r.rows()[0])  # noqa: SLF001
    assert len(r.rows()) == 1, "a phase update is not a new row"


def test_a_finished_subagent_leaves():
    r = LiveRegion()
    r.subagent_started("s1", "verify")
    r.subagent_finished("s1")
    assert r.rows() == []


def test_phase_on_an_unknown_run_is_harmless():
    """Events can outlive their row — a finish can land before a late phase."""
    LiveRegion().subagent_phase("gone", "Run")


# ── Degradation ──────────────────────────────────────────────────────────────

def test_rows_render_without_rich(monkeypatch):
    """`ui` is optional by design: Forge installs with three packages and the
    terminal getting nicer must not be why a peer cannot start."""
    monkeypatch.setattr(ui, "AVAILABLE", False)
    monkeypatch.setattr(ui, "console", lambda: None)
    r = _region(alpha=3.0)
    line = ansi._ESCAPE.sub("", r.rows()[0])  # noqa: SLF001
    assert "Alpha" in line and "3s" in line


# ── It substitutes for Spinner where the renderer expects one ────────────────

def test_it_answers_the_whole_spinner_surface():
    """`render.py`, `repl.py` and the oracle all hold this as a `Spinner`. A
    missing method is an AttributeError mid-turn, which is the worst possible
    place for one."""
    r = LiveRegion()
    for name in ("start", "stop", "clear", "pause", "resume",
                 "set_status", "add_chars", "stalled_for"):
        assert hasattr(r, name), name


def test_status_and_tokens_reach_the_header():
    r = LiveRegion()
    r.spinner.start_clock()
    r.set_status("Running run_command")
    r.add_chars(8_000)
    header = ansi._ESCAPE.sub("", r.render()[0])  # noqa: SLF001
    assert "Running run_command" in header
    assert "2.0k tokens" in header


def test_pausing_clears_the_region():
    """Streamed prose leaves the cursor mid-line; a frame drawn over it starts
    the reply in the middle of its own first word."""
    r = _region(alpha=1.0)
    r._drawn = 2
    cleared: list[int] = []
    real = ansi.rewind
    ansi.rewind = lambda n: (cleared.append(n), True)[1]
    try:
        r.pause()
    finally:
        ansi.rewind = real
    assert cleared == [2]
    assert r._paused is True


def test_stopping_one_that_never_started_is_harmless():
    asyncio.run(LiveRegion().stop())


def test_a_broken_row_does_not_end_the_turn():
    """A live region is a view. It must never be the reason a turn dies."""
    r = LiveRegion()
    r._rows["bad"] = Row(key="bad", label=None)  # type: ignore[arg-type]
    try:
        r.rows()
    except Exception:  # noqa: BLE001
        pass  # what must NOT happen is it escaping _run; see test below

    async def scenario():
        region = LiveRegion()
        region._rows["bad"] = Row(key="bad", label=None)  # type: ignore[arg-type]
        region.start()
        await asyncio.sleep(0.3)
        await region.stop()

    asyncio.run(scenario())   # must not raise
