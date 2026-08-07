"""Telling the operator a long turn has finished, when they are not watching.

A ten-minute turn currently ends in silence, which means the operator either
watches the whole thing or finds out later. That is the difference between a
tool you supervise and one you can leave running, and it costs four bytes.

**Only for turns long enough to have walked away from.** A bell on every
two-second reply is an alarm that fires when nothing happened, and the reliable
response to that is to turn the sound off — which costs the signal in the one
case it was built for. `MIN_SECONDS` is the whole policy.

Three channels, tried in order of how much they can say. The terminal-specific
ones put a real notification in the OS notification centre; `BEL` only flags the
window, but it works essentially everywhere including inside tmux and screen.
"""
from __future__ import annotations

import os
import sys

# Under this, the operator was watching. Chosen rather than measured: it is
# roughly the point past which people switch windows, and the cost of being
# slightly wrong either way is one bell nobody needed or one they wanted.
MIN_SECONDS = 30.0

BEL = "\x07"


def _term() -> str:
    return (os.environ.get("TERM_PROGRAM")
            or os.environ.get("TERM")
            or "").lower()


def sequence(title: str, message: str) -> str:
    """The escape sequence this terminal will understand, or a bare BEL.

    Pure so it can be tested without a terminal, and so the channel choice is
    inspectable rather than buried in a write.

    The BEL is emitted RAW, never wrapped for a multiplexer. Inside tmux a raw
    BEL sets the window's bell flag, which is the entire fallback — wrapping it
    would make it opaque payload and lose the one thing that still works when
    the OSC does not.
    """
    term = _term()
    if "iterm" in term:
        # OSC 9 — iTerm2's notification. It has no title field; the title is
        # folded into the body rather than dropped.
        body = f"{title}: {message}" if title else message
        return f"\x1b]9;{body}\x07"
    if "kitty" in term:
        # OSC 99. `d=1` closes the notification payload, `a=focus` asks kitty to
        # focus the window when it is clicked.
        return (f"\x1b]99;i=forge:d=0:p=title;{title}\x07"
                f"\x1b]99;i=forge:d=1:p=body;{message}\x07")
    if "ghostty" in term:
        return f"\x1b]777;notify;{title};{message}\x07"
    return BEL


def finished(seconds: float, summary: str = "", *, force: bool = False) -> bool:
    """Notify that a turn ended. True if anything was actually emitted.

    Silent when the turn was short, when stdout is not a terminal (a bell
    written into a log file is a stray byte in somebody's transcript), and when
    the operator has said not to.
    """
    if os.environ.get("FORGE_NO_BELL"):
        return False
    if not force and seconds < MIN_SECONDS:
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False

    message = summary or f"finished after {int(seconds)}s"
    try:
        sys.stdout.write(sequence("Forge", message))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a notification must never end a session
        return False
    return True
