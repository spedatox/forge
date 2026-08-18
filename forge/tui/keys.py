"""Reading one keypress, without a line editor.

The completion menu proves an arrow-driven list is the right shape for a
choice, but it is built on prompt_toolkit — and prompt_toolkit is exactly what
fails to construct in some terminals, which is the situation a permission
prompt most needs to survive. So this reads keys from the console directly:
`msvcrt` on Windows, `termios` raw mode on POSIX. Both ship with Python.

It degrades honestly. Where a key cannot be read one at a time — a pipe, no
tty, a platform with neither module — `read_key` returns None immediately and
the caller falls back to a typed line rather than blocking forever on a
terminal that will never deliver a keystroke.
"""
from __future__ import annotations

import sys
import time

UP = "up"
DOWN = "down"
ENTER = "enter"
CANCEL = "cancel"

NOTHING = "nothing"
"""Nobody pressed anything before the caller's deadline.

Distinct from None, which means this terminal will never deliver a key at all.
The difference decides what the caller does next: None is "fall back to a typed
line", NOTHING is "still waiting, go round again" — and a caller that cannot
tell them apart either abandons a working prompt or blocks on a dead one."""

_POLL_S = 0.02
"""How often a bounded read looks for a keypress. Short enough that ctrl+c at a
permission prompt feels immediate, long enough that waiting costs nothing."""


def available() -> bool:
    """Can this terminal give us keys one at a time?"""
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            import msvcrt  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return True
    except ImportError:
        return False


def read_key_raw(timeout: float | None = None) -> str | None:
    """`read_key`, but with the character exactly as typed.

    The lowercasing in `read_key` is right for a permission prompt, where `Y`
    and `y` are the same answer and treating them differently would be a bug.
    It is wrong for anything the operator is composing prose in, where it
    silently makes capitals impossible to type — so the composer takes this door
    instead of the caller having to remember to un-lowercase, which is not
    something a caller can do (`y` and `Y` are the same string by then).

    Backspace is passed through rather than named, because the one caller that
    wants it already knows both bytes and no other caller wants it at all."""
    return read_key(timeout, lower=False)


def read_key(timeout: float | None = None, *, lower: bool = True) -> str | None:
    """One keypress as a name, a single character, NOTHING, or None.

    Returns UP/DOWN/ENTER/CANCEL for the navigation keys, otherwise the
    lowercased character, so a caller can accept both `↓ ↵` and a typed `a`
    without knowing which arrived.

    **`timeout` is what makes a prompt abandonable.** An unbounded read here is
    a hold on the whole harness: the read runs in a worker thread, the turn is
    parked inside one tool dispatch waiting for it, and the interrupt signal is
    only ever checked at a loop boundary the turn can no longer reach. Ctrl+c
    then does nothing at all — observed as Forge getting stuck running a
    command, which is exactly the tool the gate stops. With a deadline the
    caller gets the loop back a few times a second and can notice it has been
    told to stop.

    The deadline is also why this polls rather than reading and discarding: a
    read already in flight consumes the next keypress whenever it finally
    arrives, and that key would be stolen from the prompt the operator has
    since returned to.
    """
    if not available():
        return None
    try:
        if sys.platform == "win32":
            return _read_win(timeout, lower=lower)
        return _read_posix(timeout, lower=lower)
    except Exception:  # noqa: BLE001 — an unreadable key is a fallback, not a crash
        return None


def _wait_win(timeout: float | None) -> bool:
    """True once a key is sitting in the console buffer. False on expiry."""
    import msvcrt

    if timeout is None:
        while not msvcrt.kbhit():
            time.sleep(_POLL_S)
        return True
    deadline = time.monotonic() + timeout
    while not msvcrt.kbhit():
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
    return True


def _read_win(timeout: float | None = None, *, lower: bool = True) -> str | None:
    import msvcrt

    if not _wait_win(timeout):
        return NOTHING
    ch = msvcrt.getwch()
    # Arrows arrive as a two-character sequence led by NUL or 0xE0; the second
    # character is the actual key and MUST be consumed either way, or it is
    # read next time as a stray letter ('H' would look like the operator typed
    # h).
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return {"H": UP, "P": DOWN}.get(code, "")
    if ch in ("\r", "\n"):
        return ENTER
    if ch in ("\x03", "\x1b"):        # ctrl-c, escape
        return CANCEL
    return ch.lower() if lower else ch


def _read_posix(timeout: float | None = None, *, lower: bool = True) -> str | None:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        # Raw mode BEFORE the wait, not after. A tty in canonical mode reports
        # itself readable only once a whole line has been entered, so polling a
        # cooked terminal would sit through every single keypress and wake only
        # on Enter — which is not a keypress reader at all.
        tty.setraw(fd)
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return NOTHING
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # CSI: ESC [ A/B. Read the rest so it cannot be mistaken for the
            # operator typing '[' and 'A'. A bare ESC (nothing follows) is a
            # cancel, which is why the terminal is left in raw mode for the
            # peek rather than restored between reads.
            nxt = sys.stdin.read(1)
            if nxt != "[":
                return CANCEL
            return {"A": UP, "B": DOWN}.get(sys.stdin.read(1), "")
        if ch in ("\r", "\n"):
            return ENTER
        if ch == "\x03":
            return CANCEL
        return ch.lower() if lower else ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
