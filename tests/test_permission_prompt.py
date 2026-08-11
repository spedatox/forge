"""The permission prompt has to be usable.

Reported as "hitting y / n / always does nothing". The keystrokes were arriving
the whole time — what failed was the screen. A subagent's spinner repaints with
a carriage return and a clear-to-end-of-line several times a second, and the
prompt was printed into that. The question, the `>` and every character typed
into it were erased between frames.

Nothing about permissions was broken. The operator simply had no way to see
that anything was working, which for an interactive prompt is the same thing.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tui.session import TerminalOracle


class _Spinner:
    def __init__(self):
        self.events: list[str] = []

    def pause(self):
        self.events.append("pause")

    def resume(self):
        self.events.append("resume")


def _answer(monkeypatch, typed: str):
    monkeypatch.setattr("builtins.input", lambda *a: typed)


def test_the_live_line_is_stopped_while_the_question_is_up(monkeypatch):
    """The bug. Without this the prompt is erased between spinner frames."""
    _answer(monkeypatch, "y")
    sp = _Spinner()
    oracle = TerminalOracle(sp)

    asyncio.run(oracle.ask("read_file", ".env", "protected location"))

    assert sp.events == ["pause", "resume"]


def test_the_line_resumes_even_if_the_operator_interrupts(monkeypatch):
    """Ctrl-C at the prompt is a refusal, not a reason to leave the display
    dead for the rest of the turn."""
    def _boom(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _boom)
    sp = _Spinner()

    answer = asyncio.run(TerminalOracle(sp).ask("run_command", "rm -rf /", "destructive"))

    assert answer.approved is False
    assert sp.events == ["pause", "resume"]


def test_it_still_works_with_no_spinner(monkeypatch):
    """The peer path and the tests construct one without a live line."""
    _answer(monkeypatch, "y")
    assert asyncio.run(TerminalOracle().ask("t", "k", "r")).approved


@pytest.mark.parametrize("typed", ["y", "yes", "once", "1", "Y", " y "])
def test_yes_is_accepted_generously(monkeypatch, typed):
    _answer(monkeypatch, typed)
    answer = asyncio.run(TerminalOracle().ask("t", "k", "r"))
    assert answer.approved and not answer.remember


@pytest.mark.parametrize("typed", ["a", "always", "2", "ALWAYS"])
def test_always_grants_a_standing_permission(monkeypatch, typed):
    _answer(monkeypatch, typed)
    answer = asyncio.run(TerminalOracle().ask("t", "k", "r"))
    assert answer.approved and answer.remember


def test_a_bare_enter_refuses(monkeypatch):
    """The likeliest accident at a prompt someone is still reading, so it must
    land on "nothing happened" rather than "one call happened".

    Written after I changed this to approve-once and the existing suite caught
    it: approval on a permission gate has to cost a deliberate keystroke, or
    the gate is a formality."""
    _answer(monkeypatch, "")
    assert asyncio.run(TerminalOracle().ask("t", "k", "r")).approved is False


@pytest.mark.parametrize("typed", ["n", "no", "nope", "q", "anything else"])
def test_anything_unrecognised_is_a_refusal(monkeypatch, typed):
    """The safe default for an answer nobody understood is no."""
    _answer(monkeypatch, typed)
    assert asyncio.run(TerminalOracle().ask("t", "k", "r")).approved is False


def test_each_option_carries_its_consequence(monkeypatch, capsys):
    """`[y] once [a] always [n] no` asks someone to hold three letter-meanings
    in their head under time pressure — and `a` is the consequential one."""
    _answer(monkeypatch, "y")
    asyncio.run(TerminalOracle().ask("read_file", ".env", "protected"))

    out = capsys.readouterr().out
    assert "Yes" in out
    assert "don't ask again" in out       # the scope, not just a word
    assert "tell it what to do instead" in out


# ── inherited from Claude Code's permission dialog ───────────────────────────


def test_the_standing_permission_says_what_it_grants():
    """"always" told the operator nothing about scope. Forge records the EXACT
    action string and never a pattern, so the label says exactly that — a
    standing permission is the one answer nobody should agree to from a word
    they had to interpret."""
    from forge.tui.session import _CHOICES

    always = next(c for c in _CHOICES if c[0] == "always")
    assert "don't ask again" in always[2]
    assert "this exact action" in always[2]


def test_refusing_can_carry_an_instruction(monkeypatch):
    """A bare "no" tells the agent it may not do this and nothing about what to
    do instead, so it guesses — and the likeliest guess is a way around the
    refusal."""
    answers = iter(["t", "read .env.example instead"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    answer = asyncio.run(TerminalOracle().ask("read_file", ".env", "protected"))

    assert answer.approved is False
    assert "read .env.example instead" in answer.note


def test_that_instruction_reaches_the_model():
    """The channel already existed — dispatch puts answer.note into the denial
    the model reads. Nothing was ever put into it."""
    import inspect

    from forge.warden import dispatch

    src = inspect.getsource(dispatch)
    assert "The operator declined this: {answer.note" in src


def test_an_empty_redirect_is_just_a_refusal(monkeypatch):
    """Sending the model an empty string to interpret is worse than sending it
    the plain refusal."""
    answers = iter(["t", "   "])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    answer = asyncio.run(TerminalOracle().ask("t", "k", "r"))

    assert answer.approved is False
    assert answer.note == "declined at the prompt"


def test_the_cursor_starts_on_refuse():
    """On a gate, the answer reached by the least deliberate keystroke should
    be the one that does nothing."""
    from forge.tui.session import _CHOICES

    assert _CHOICES[-1][0] == "redirect"
    # the plain refusal sits immediately before it, and the selector opens
    # on the last index, so Enter never approves by accident
    assert "no" in {c[0] for c in _CHOICES}


def test_arrow_keys_are_read_without_the_line_editor():
    """The completion menu is prompt_toolkit, which is exactly what fails to
    build in some terminals — the situation a permission prompt most needs to
    survive."""
    from forge.tui import keys

    assert hasattr(keys, "read_key") and hasattr(keys, "available")
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(keys))
    imported = {
        (n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    } | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    # Checked against the imports, not the source text — the module docstring
    # names prompt_toolkit precisely to explain why it is NOT used, and a grep
    # would fail on the explanation.
    assert not any("prompt_toolkit" in m for m in imported)


# ── A prompt nobody answers must not hold the whole harness ──────────────────
#
# Reported as "Forge sometimes gets stuck running a command", and `run_command`
# is exactly the tool the gate stops. The keystroke reader blocks in a worker
# thread; the turn is parked inside that one tool dispatch; and `_run_turn`
# answers ctrl+c by setting the interrupt event and then awaiting the loop —
# which can no longer reach a boundary where the event would be checked. The
# operator pressed ctrl+c and nothing happened, for as long as they let it.


def test_ctrl_c_reaches_a_prompt_that_is_waiting(monkeypatch):
    """The hang. The prompt is the only thing that can notice, because it is
    what the turn is parked on."""
    from forge.tui import keys
    from forge.tui.session import TerminalOracle

    signal = asyncio.Event()
    monkeypatch.setattr(keys, "available", lambda: True)

    def _never_pressed(timeout=None):
        # Whatever deadline the caller set, expired. Nobody is at the keyboard.
        assert timeout is not None, "an unbounded read is the bug itself"
        signal.set()                       # ctrl+c, mid-wait
        return keys.NOTHING

    monkeypatch.setattr(keys, "read_key", _never_pressed)

    oracle = TerminalOracle(signal=signal)
    answer = asyncio.wait_for(
        oracle.ask("run_command", "rm -rf build", "destructive"), timeout=5)
    answer = asyncio.run(answer)

    assert answer.approved is False
    assert "interrupted" in answer.note


def test_an_interrupted_run_is_not_the_operator_saying_no(monkeypatch):
    """Both end with the action not happening and they read differently: "the
    operator declined this" invites the model to find another way, and a
    stopped run does not."""
    from forge.tui.session import TerminalOracle

    signal = asyncio.Event()
    signal.set()
    answer = asyncio.run(TerminalOracle(signal=signal).ask("run_command", "x", "y"))

    assert answer.approved is False
    assert "declined" not in answer.note
    assert "interrupted" in answer.note


def test_an_interrupted_run_does_not_stop_to_ask_a_question():
    """`ask_operator` parks the same way, and an unanswered question already
    means "decide for yourself" — so an interrupted one costs nothing."""
    from forge.tui.session import TerminalOracle

    signal = asyncio.Event()
    signal.set()
    reply = asyncio.run(TerminalOracle(signal=signal).consult("REST or WebSocket?"))

    assert reply.answered is False


def test_a_prompt_with_no_interrupt_attached_still_blocks_normally(monkeypatch):
    """The peer path and every test construct an oracle without a signal. That
    has to keep meaning "wait for the answer", not "give up immediately"."""
    _answer(monkeypatch, "y")
    assert asyncio.run(TerminalOracle().ask("t", "k", "r")).approved


def test_the_turn_hands_its_interrupt_to_the_oracle():
    """The wiring, without which everything above is unreachable in the app."""
    import inspect

    from forge.tui import repl

    src = inspect.getsource(repl._run_turn)
    assert "session.oracle.signal = signal" in src


def test_a_bounded_read_reports_expiry_distinctly():
    """NOTHING and None must not collapse into one another: None means this
    terminal will never deliver a key and the caller should fall back to a
    typed line, NOTHING means keep waiting. A caller that cannot tell them
    apart either abandons a working prompt or blocks on a dead one."""
    from forge.tui import keys

    assert keys.NOTHING is not None
    assert keys.NOTHING not in (keys.UP, keys.DOWN, keys.ENTER, keys.CANCEL)
