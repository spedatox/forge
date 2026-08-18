"""Choosing one thing from a grouped list, with the keys already learned here.

The permission prompt (`session.select`) proved the shape: an arrow-driven list
read straight from the console, so it survives terminals where prompt_toolkit
will not construct. It is also hardcoded to four fixed answers, which is the
right call there and no use to a list of ninety models across five providers.

What a long list needs on top of that shape is three things: **groups**, so the
provider owning a model is a heading rather than a repeated prefix; **typing to
narrow**, because reaching `gpt-5.1` with the down arrow eighty times is not
choosing; and **a window**, so a list longer than the terminal scrolls instead
of painting off the top of the screen.

It degrades the same way everything else in this package does. Without a tty —
a pipe, a CI run, a terminal with neither msvcrt nor termios — it prints the
options numbered and reads one line, which is a worse experience and a working
one.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from forge.tui import ansi, keys

_CHROME_ROWS = 4
"""Rows the frame spends on furniture: title, filter line, and the key hints.
Subtracted from the terminal height to size the scrolling window."""

MIN_WINDOW = 4
"""Never show fewer options than this, whatever the terminal claims its height
is. A 6-row terminal is almost certainly a lie told by a pipe, and a one-line
window is unusable in the case where it is true."""


@dataclass(frozen=True)
class Option:
    """One selectable row. `value` is what the caller gets back."""

    value: str
    label: str = ""             # defaults to the value
    hint: str = ""              # dimmed, right of the label

    @property
    def text(self) -> str:
        return self.label or self.value

    def matches(self, query: str) -> bool:
        """Substring over everything visible, so typing 'sonnet' finds
        `anthropic:claude-sonnet-4-6` and typing '4-6' finds it too."""
        if not query:
            return True
        hay = f"{self.value} {self.label} {self.hint}".lower()
        return all(part in hay for part in query.lower().split())


@dataclass(frozen=True)
class Group:
    """A heading and its options. `note` replaces the options when they are
    absent — an error from that provider, most of the time, which is worth a
    row of its own rather than the group silently vanishing."""

    title: str
    options: tuple[Option, ...] = ()
    note: str = ""


@dataclass
class _Row:
    """A rendered line: either a heading/note (not selectable) or an option."""

    text: str
    option: Option | None = None
    styles: tuple[str, ...] = field(default_factory=tuple)


def _rows(groups: list[Group], query: str) -> list[_Row]:
    """Flatten groups to rows, dropping groups nothing in them matches.

    A heading with no rows under it is noise while filtering — five providers'
    worth of it, on every keystroke. A group whose *note* is its content stays,
    because an error is still an answer to "what can I pick here".
    """
    out: list[_Row] = []
    for group in groups:
        hits = [o for o in group.options if o.matches(query)]
        if not hits and not (group.note and not query):
            continue
        out.append(_Row(text=group.title, styles=("bold", "cyan")))
        if group.note and not hits:
            out.append(_Row(text=f"  {group.note}", styles=("dim",)))
        for option in hits:
            out.append(_Row(text=option.text, option=option))
    return out


def _window(rows: list[_Row], cursor: int, height: int) -> tuple[int, int]:
    """The slice of `rows` to draw, keeping the cursor inside it.

    The cursor is centred once the list is longer than the window, rather than
    pinned to an edge: an operator arrowing down a long list wants to see what
    is coming, and a cursor glued to the last row shows only where they have
    been.
    """
    if len(rows) <= height:
        return 0, len(rows)
    start = max(0, min(cursor - height // 2, len(rows) - height))
    return start, start + height


def _frame(rows: list[_Row], cursor: int, query: str, title: str,
           current: str, height: int) -> list[str]:
    start, end = _window(rows, cursor, height)
    lines: list[str] = []
    if title:
        head = f"  {title}"
        if current:
            head += ansi.paint(f"   (now: {current})", "dim")
        lines.append(head)
    for index in range(start, end):
        row = rows[index]
        if row.option is None:
            lines.append("  " + ansi.paint(row.text, *(row.styles or ("bold",))))
            continue
        hint = f"  {ansi.paint(row.option.hint, 'dim')}" if row.option.hint else ""
        if index == cursor:
            lines.append("  " + ansi.paint(f"❯ {row.text}", "bold", "cyan") + hint)
        else:
            lines.append(f"    {row.text}{hint}")
    # Which direction the rest of the list is in, not just how much of it there
    # is: "5 more" above a window scrolled into the middle reads as five rows
    # below, and an operator who has already passed what they wanted scrolls
    # further away from it.
    above, below = start, len(rows) - end
    if above or below:
        parts = ([f"↑ {above} above"] if above else []) + \
                ([f"↓ {below} below"] if below else [])
        lines.append(ansi.paint("    " + " · ".join(parts), "dim"))
    lines.append(ansi.paint(f"    filter: {query}▏" if query
                            else "    ↑↓ choose · type to filter · enter select · esc cancel",
                            "dim"))
    return lines


def _first_option(rows: list[_Row], start: int = 0) -> int:
    for index in range(start, len(rows)):
        if rows[index].option is not None:
            return index
    return -1


def _step(rows: list[_Row], cursor: int, delta: int) -> int:
    """Move to the next selectable row, wrapping, skipping headings."""
    if not rows:
        return cursor
    index = cursor
    for _ in range(len(rows)):
        index = (index + delta) % len(rows)
        if rows[index].option is not None:
            return index
    return cursor


def _select_sync(groups: list[Group], title: str, current: str) -> str | None:
    """The interactive path. Returns the chosen value, or None for cancelled.

    Repainted through `ansi.repaint`, which draws the whole frame in one write
    and leaves no blank moment between frames — the same reason the live region
    uses it. A list that blinks on every arrow key reads as a bug in the
    terminal.
    """
    query = ""
    rows = _rows(groups, query)
    cursor = _first_option(rows)
    if cursor < 0:
        return None
    # Start on what is already selected, so enter is a no-op and the operator
    # can see where they are before moving.
    for index, row in enumerate(rows):
        if row.option is not None and row.option.value == current:
            cursor = index
            break

    height = max(MIN_WINDOW, ansi.terminal_height() - _CHROME_ROWS)
    painted = ansi.repaint(_frame(rows, cursor, query, title, current, height), 0)
    while True:
        key = keys.read_key_raw()
        if key is None:                       # the console stopped cooperating
            ansi.repaint([], painted)
            return None
        if key == keys.CANCEL:
            ansi.repaint([], painted)
            return None
        if key == keys.ENTER:
            chosen = rows[cursor].option
            ansi.repaint([], painted)
            return chosen.value if chosen else None
        if key in (keys.UP, keys.DOWN):
            cursor = _step(rows, cursor, -1 if key == keys.UP else 1)
        elif key in ("\x7f", "\x08"):          # backspace, either dialect
            query = query[:-1]
            rows = _rows(groups, query)
            cursor = _first_option(rows)
        elif len(key) == 1 and key.isprintable():
            candidate = query + key
            narrowed = _rows(groups, candidate)
            # A keystroke that would empty the list is refused rather than
            # obeyed: an empty frame gives the operator nothing to correct
            # from, and the alternative — accept it, show nothing — makes
            # backspace the only working key on a screen that looks broken.
            if _first_option(narrowed) >= 0:
                query, rows = candidate, narrowed
                cursor = _first_option(rows)
        else:
            continue                           # unknown escape sequence
        if cursor < 0:
            cursor = 0
        painted = ansi.repaint(
            _frame(rows, cursor, query, title, current, height), painted)


def _typed_sync(groups: list[Group], title: str) -> str | None:
    """No tty: numbered options and one line of input.

    Accepts the number or the value itself, because a caller who knows the ref
    they want should not have to count rows to say so.
    """
    if title:
        ansi.write(f"  {title}")
    numbered: list[Option] = []
    for group in groups:
        ansi.write("  " + ansi.paint(group.title, "bold"))
        if not group.options and group.note:
            ansi.write(ansi.paint(f"    {group.note}", "dim"))
        for option in group.options:
            numbered.append(option)
            hint = f"  {option.hint}" if option.hint else ""
            ansi.write(f"    {len(numbered):>3}  {option.text}{hint}")
    if not numbered:
        return None
    try:
        raw = input("  number or name > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(numbered):
        return numbered[int(raw) - 1].value
    for option in numbered:
        if raw == option.value or raw == option.text:
            return option.value
    hits = [o for o in numbered if o.matches(raw)]
    return hits[0].value if len(hits) == 1 else None


async def pick(groups: list[Group], *, title: str = "",
               current: str = "") -> str | None:
    """Choose one option. None means cancelled, or nothing to choose from.

    Off the event loop, like the pager and the permission prompt: the read
    blocks until a key arrives, and everything else the loop is doing would
    block with it for as long as the operator takes to decide.
    """
    if not any(g.options for g in groups):
        return None
    # Both halves are required, and for different reasons: without key reading
    # there is nothing to drive the cursor with, and without escape sequences
    # `repaint` draws nothing at all — which would leave the operator pressing
    # arrows at a blank screen rather than at a list.
    if keys.available() and ansi.styled():
        return await asyncio.to_thread(_select_sync, groups, title, current)
    return await asyncio.to_thread(_typed_sync, groups, title)
