"""The six UX mechanisms taken from the reference's terminal UI.

Each one exists because the absence of it is invisible: a stalled connection
looks like a thinking model, a finished job looks like a running one, a folded
paste looks like a complete one. These assert the distinction is now drawn.
"""
import time
from pathlib import Path

from forge.tui import input as input_mod
from forge.tui import notify, status
from forge.tui.spinner import STALL_AFTER_S, Spinner


def _primed() -> Spinner:
    """A spinner with its clock running but no draw task.

    `start()` needs a live event loop to schedule the redraw; every property
    under test here is pure timing state, so priming it directly keeps these
    synchronous."""
    s = Spinner()
    s._started = time.monotonic()
    s._last_progress = s._started
    return s


# ── 1. The live line distinguishes stalled from working ──────────────────────

def test_spinner_reports_a_stall():
    line = _primed().render(now=10.0, stalled_for=STALL_AFTER_S + 3)
    assert "nothing received" in line


def test_spinner_says_nothing_while_tokens_flow():
    assert "nothing received" not in _primed().render(now=10.0, stalled_for=0.0)


def test_a_running_tool_is_never_a_stall():
    """A two-minute test suite is working. Colouring it red teaches the eye to
    ignore the colour, which costs the signal in the case it was built for."""
    s = _primed()
    s.set_status("Running run_command")
    s._last_progress = time.monotonic() - 600
    assert s.stalled_for == 0.0


def test_the_stall_clock_restarts_when_the_batch_ends():
    """After the last tool result the loop calls the model again, and THAT wait
    is worth watching. If the tool flag survived, it would not be."""
    s = _primed()
    s.set_status("Running run_command")
    assert s.stalled_for == 0.0
    s.set_status("Thinking")            # what the renderer sends at batch end
    s._last_progress = time.monotonic() - 60
    assert s.stalled_for > STALL_AFTER_S


def test_streamed_characters_count_as_progress():
    s = _primed()
    s._last_progress = time.monotonic() - 60
    s.add_chars(120)
    assert s.stalled_for < 1.0


# ── 2. Completion notification ───────────────────────────────────────────────

def test_short_turns_are_silent():
    """A bell on every two-second reply is an alarm that fires when nothing
    happened, and the reliable response to that is to switch the sound off."""
    assert notify.finished(2.0) is False


def test_notification_respects_the_opt_out(monkeypatch):
    monkeypatch.setenv("FORGE_NO_BELL", "1")
    assert notify.finished(600.0, force=True) is False


def test_notification_is_silent_when_not_a_terminal():
    """A bell written into a log file is a stray byte in somebody's transcript.
    pytest captures stdout, so this is the ambient case here."""
    assert notify.finished(600.0) is False


def test_bell_is_the_fallback_channel(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert notify.sequence("Forge", "done") == notify.BEL


def test_known_terminals_get_a_real_notification(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    seq = notify.sequence("Forge", "done")
    assert seq != notify.BEL and "done" in seq


# ── 4. Context pressure is announced on the crossing, once ───────────────────

class _Ledger:
    def __init__(self, fullness: float) -> None:
        self.fullness = fullness
        self.turns = 3


class _Session:
    def __init__(self, fullness: float) -> None:
        self.ledger = _Ledger(fullness)
        self.context_warned = False


def test_no_warning_below_the_line():
    assert status.pressure_warning(_Session(0.4)) == ""


def test_warning_fires_on_the_crossing():
    session = _Session(status.WARN_FULLNESS + 0.05)
    assert "context is" in status.pressure_warning(session)


def test_warning_does_not_repeat():
    """The percentage is already on the status line every turn. Repeating the
    crossing would make it a second gauge rather than an event."""
    session = _Session(0.9)
    assert status.pressure_warning(session) != ""
    assert status.pressure_warning(session) == ""


def test_reclaiming_context_re_arms_the_warning():
    session = _Session(0.9)
    status.pressure_warning(session)
    status.forget_pressure(session)
    assert status.pressure_warning(session) != ""


# ── 6. Oversized pastes are folded, and say where the rest went ──────────────

def test_small_input_is_untouched():
    text = "fix the retry logic"
    assert input_mod.fold_paste(text) == (text, "")


def test_large_paste_keeps_both_ends():
    text = "HEAD" + ("x" * 40_000) + "TAIL"
    folded, withheld = input_mod.fold_paste(text)
    assert folded.startswith("HEAD")
    assert folded.endswith("TAIL")
    assert withheld
    assert len(folded) < len(text)


def test_folded_paste_counts_lines_not_characters():
    text = "a\n" * 20_000
    folded, _ = input_mod.fold_paste(text)
    assert "lines elided" in folded


def test_folded_paste_names_the_spill_file(tmp_path: Path):
    """Withholding text and saying nothing about where it went would make the
    message a quiet lie about what was provided."""
    spill = tmp_path / "nested" / "paste.txt"
    text = "HEAD" + ("y" * 40_000) + "TAIL"
    folded, _ = input_mod.fold_paste(text, spill_path=spill)

    assert str(spill) in folded
    assert spill.read_text(encoding="utf-8") == text, "the spill holds the WHOLE paste"


def test_unwritable_spill_still_folds(tmp_path: Path):
    """A read-only workspace loses the path, not the fold."""
    spill = tmp_path / "file.txt" / "impossible.txt"
    (tmp_path / "file.txt").write_text("blocking", encoding="utf-8")
    text = "HEAD" + ("z" * 40_000) + "TAIL"
    folded, withheld = input_mod.fold_paste(text, spill_path=spill)

    assert withheld
    assert "lines elided" in folded
    assert "full text is at" not in folded


# ── 5. ctrl-c twice leaves ───────────────────────────────────────────────────

class _Bar:
    """Just the double-press state — building a real InputBar needs a tty."""
    _last_interrupt = 0.0
    _interrupt_again = input_mod.InputBar._interrupt_again


def test_one_interrupt_does_not_exit(capsys):
    bar = _Bar()
    assert bar._interrupt_again() is False
    assert "again to exit" in capsys.readouterr().out


def test_two_interrupts_exit():
    bar = _Bar()
    bar._interrupt_again()
    assert bar._interrupt_again() is True


def test_a_slow_second_interrupt_is_a_fresh_first(capsys):
    """The same keystroke is how people abandon a half-typed line. Exiting on
    two presses a minute apart would lose a session to a typo correction."""
    bar = _Bar()
    bar._interrupt_again()
    bar._last_interrupt = time.monotonic() - (input_mod.DOUBLE_PRESS_S + 1)
    assert bar._interrupt_again() is False
