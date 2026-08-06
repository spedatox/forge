"""Terminal output primitives — stdlib only.

No `rich`, no `textual`. Forge's install is pydantic + websockets + anthropic,
and a local test surface is not a good reason to make that four. Everything here
is ANSI escapes, which every terminal Forge will run in supports — including
Windows 10's console once virtual-terminal processing is switched on, which
`enable()` does.

Colour is a signal, not decoration: it distinguishes what the model *said* from
what the harness *did*, which is the one distinction a transcript must never
blur. When the terminal cannot do colour — a pipe, a dumb TERM, NO_COLOR set —
every function degrades to plain text and nothing is lost but the shading.
"""
from __future__ import annotations

import os
import re
import sys

_ENABLED = False
_UNICODE = False

# Every non-ASCII glyph the TUI uses, with a plain fallback. Windows consoles
# still default to cp1252, which cannot encode any of the left column — and an
# UnicodeEncodeError while drawing a banner would take down the session before
# the operator typed anything. The fallbacks are not decorative equivalents;
# they are chosen so the layout still parses at a glance.
GLYPHS = {
    # Chosen for FONT coverage, not for looking clever. A terminal that can
    # encode a character still renders a hollow box when the font has no glyph
    # for it, and tofu in the layout is worse than a plainer character that
    # draws. Box-drawing and geometric shapes are in essentially every
    # monospace font; the dingbats and technical symbols are not.
    "▲": "#", "●": "*", "└": "\\_", "─": "-", "│": "|",
    "╭": ".", "╮": ".", "╰": "'", "╯": "'",
    "✗": "x", "◆": "~", "⚠": "!!", "⏹": "[]", "→": "->",
    "█": "#", "░": ".", "⏎": "\n", "…": "...", "·": ".", "›": ">", "═": "=",
    "◐": "|", "◓": "/", "◑": "-", "◒": "\\",
    "—": "-", "↑": "^", "↓": "v", "⇄": "/", "⏶": "^",
    # The permission selector's cursor. ">" rather than "*" because the row it
    # marks is a choice being pointed at, not an item in a list.
    "❯": ">",
}


def enable() -> bool:
    """Turn on colour if the terminal will take it. Idempotent.

    Also settles whether the terminal can render the box-drawing glyphs, and
    upgrades the stream to UTF-8 when Python allows — the console usually can
    display them once it stops being asked in cp1252."""
    global _ENABLED, _UNICODE
    _UNICODE = _probe_unicode()
    if _ENABLED:
        return True
    if os.environ.get("NO_COLOR") is not None:      # no-color.org
        return False
    if not sys.stdout.isatty():
        return False                                 # piped: keep the bytes clean
    if os.environ.get("TERM") == "dumb":
        return False
    if sys.platform == "win32":
        # Windows 10 1511+ can do ANSI but does not by default. Ask the console
        # for it; if the call fails we are on something older and stay plain.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            mode = ctypes.c_uint32()
            handle = kernel32.GetStdHandle(-11)      # STD_OUTPUT_HANDLE
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return False
        except Exception:  # noqa: BLE001 — any failure means "no colour", not "no Forge"
            return False
    _ENABLED = True
    return True


# ── Palette ──────────────────────────────────────────────────────────────────
# 256-colour, because 16 is too coarse to separate five kinds of harness output
# and truecolour is not universal.
_CODES = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "reset": "\x1b[0m",
    "cyan": "\x1b[38;5;51m",
    "blue": "\x1b[38;5;75m",
    "green": "\x1b[38;5;78m",
    "yellow": "\x1b[38;5;221m",
    "orange": "\x1b[38;5;215m",
    "red": "\x1b[38;5;203m",
    "grey": "\x1b[38;5;245m",
    "magenta": "\x1b[38;5;177m",
}


def _probe_unicode() -> bool:
    """Can this stream carry the glyphs? Ask it, rather than guessing from the
    platform — a Windows terminal set to UTF-8 handles them fine, and a Linux
    one piped through a C-locale process does not."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # py3.7+; no-op if already
    except Exception:  # noqa: BLE001 — a stream that refuses keeps its encoding
        pass
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(GLYPHS).encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def visible_width(text: str) -> int:
    """How wide a styled string actually prints.

    Colour codes are zero-width but count in `len`, so any layout that measures
    a painted string puts its column somewhere different on every row. Anything
    aligning two columns has to measure with this instead."""
    return len(_ESCAPE.sub("", text))


def unicode_ok() -> bool:
    """Whether this terminal can encode the decorative glyphs.

    Public because callers that build their OWN glyph sets — the spinner's
    frames, say — need the same answer `glyphs()` uses, and reaching into a
    module private to get it is how the two drift apart."""
    return _UNICODE


def glyphs(text: str) -> str:
    """Swap in ASCII fallbacks when the terminal cannot encode the originals."""
    if _UNICODE:
        return text
    for fancy, plain in GLYPHS.items():
        text = text.replace(fancy, plain)
    return text


def paint(text: str, *styles: str) -> str:
    """Wrap `text` in styles, or return it untouched when colour is off."""
    if not _ENABLED or not styles:
        return text
    prefix = "".join(_CODES.get(s, "") for s in styles)
    return f"{prefix}{text}{_CODES['reset']}" if prefix else text


def write(text: str = "", end: str = "\n") -> None:
    """Write a line, degrading glyphs and never raising on an encoding it cannot
    manage — output is the one thing that must not be able to end the session."""
    payload = glyphs(text) + end
    try:
        sys.stdout.write(payload)
        sys.stdout.flush()
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(payload.encode(encoding, "replace").decode(encoding, "replace"))
        sys.stdout.flush()


def transient(text: str) -> None:
    """Draw a line that will be overwritten in place — no newline, no scrollback.

    The whole basis of the live region. `\\r` returns to column 0 and `\\x1b[K`
    erases to end of line, so the next draw replaces this one instead of stacking
    up. Nothing written this way survives, which is exactly right for a spinner:
    it is a view of *now*, and a terminal full of dead spinner frames is a
    transcript nobody can read.

    A no-op when styling is off — a dumb terminal or a redirected stdout gets
    silence here rather than a screenful of escape codes in a log file.
    """
    if not _ENABLED:
        return
    write("\r\x1b[K" + text, end="")


def clear_transient() -> None:
    """Erase the live line so ordinary output can be written on top of it.

    Every real write during a turn goes through here first. Without it the
    spinner's half-line and the model's prose share a row and both become
    unreadable."""
    if not _ENABLED:
        return
    write("\r\x1b[K", end="")


def rewind(lines: int) -> bool:
    """Move the cursor up `lines` rows and erase everything below it.

    What lets a reply stream token by token AND end up rendered: the raw text
    goes out as it arrives, then this reclaims the rows it occupied and the
    formatted version is printed over them. `\\x1b[{n}A` moves up, `\\x1b[J`
    erases from the cursor to the end of the screen.

    False when it must not be attempted — no styling, or nothing to reclaim.
    The caller then leaves the raw text where it is, which is correct: a
    partial rewind would eat somebody's scrollback.
    """
    if not _ENABLED or lines <= 0:
        return False
    write(f"\r\x1b[{lines}A\x1b[J", end="")
    return True


def wrapped_height(text: str, width: int = 0) -> int:
    """How many terminal rows `text` will occupy once the terminal wraps it.

    Counting newlines is not enough — a long paragraph is one line of text and
    several rows on screen, and rewinding by the wrong number either leaves
    debris or eats the line above.
    """
    width = width or terminal_width()
    if width <= 0:
        return len(text.splitlines()) or 1
    rows = 0
    for line in text.split("\n"):
        printable = visible_width(line)
        rows += max(1, -(-printable // width))   # ceil division
    return rows


def terminal_height(default: int = 24) -> int:
    try:
        return os.get_terminal_size().lines
    except OSError:
        return default


def rule(label: str = "", width: int = 0) -> str:
    """A horizontal divider, optionally labelled."""
    width = width or min(terminal_width(), 80)
    if not label:
        return paint("─" * width, "dim")
    head = f"── {label} "
    return paint(head + "─" * max(0, width - len(head)), "dim")


def terminal_width(default: int = 80) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


def truncate(text: str, limit: int) -> str:
    """One line, bounded. Newlines become '⏎' so a multi-line command still
    reads as one row rather than silently wrapping the layout."""
    flat = text.replace("\n", " ⏎ ").strip()
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"
