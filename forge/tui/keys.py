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

UP = "up"
DOWN = "down"
ENTER = "enter"
CANCEL = "cancel"


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


def read_key() -> str | None:
    """One keypress as a name, a single character, or None if unreadable.

    Returns UP/DOWN/ENTER/CANCEL for the navigation keys, otherwise the
    lowercased character, so a caller can accept both `↓ ↵` and a typed `a`
    without knowing which arrived.
    """
    if not available():
        return None
    try:
        return _read_win() if sys.platform == "win32" else _read_posix()
    except Exception:  # noqa: BLE001 — an unreadable key is a fallback, not a crash
        return None


def _read_win() -> str | None:
    import msvcrt

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
    return ch.lower()


def _read_posix() -> str | None:
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
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
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
