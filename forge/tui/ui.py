"""The rendering layer — Rich primitives, inline.

`ansi.py` writes escape sequences into strings and computes its own padding.
That is fine for a line and does not scale to a layout: every box, column and
wrap has to be measured by hand, and the measurement is wrong the moment a
string carries a style. The reference TUI does not do that — it has a layout
engine underneath (Ink), and the difference is most of why a hand-rolled
version reads as primitive next to it.

Rich is the equivalent here. Crucially it renders **inline**, the same as Ink:
output commits to the terminal's own scrollback, so scrolling, selection and
copy keep working and the transcript survives the session. A full-screen
framework would take the alternate buffer and lose all of that.

**Additive, never load-bearing.** `ansi` remains the primitive layer and every
function here degrades to it when Rich is absent. Forge installs with pydantic,
websockets and anthropic; the terminal getting nicer must not be the reason a
peer cannot start.

**Grey, not branded.** The palette is neutral by choice — one accent for
structure, colour reserved for meaning: green and red carry diffs and failures
because that is information, everything else is a shade.
"""
from __future__ import annotations

from typing import Any

from forge.tui import ansi

try:
    from rich.console import Console, Group
    from rich.box import ROUNDED
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.style import Style
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme

    AVAILABLE = True
except ImportError:  # pragma: no cover — the degraded path
    AVAILABLE = False


# ── Palette ──────────────────────────────────────────────────────────────────
# Greys carry structure; colour is reserved for meaning. A transcript where
# everything is coloured has no emphasis left for the two things that matter —
# what changed, and what failed.
PALETTE = {
    "fg": "grey85",           # ordinary text
    "muted": "grey50",        # results, secondary detail
    "faint": "grey35",        # borders, rules, hints
    "accent": "grey100",      # the one bright: names, headings
    "added": "green",
    "removed": "red",
    "failed": "red",
    "warn": "yellow",
}

_THEME = {
    "forge.fg": PALETTE["fg"],
    "forge.muted": PALETTE["muted"],
    "forge.faint": PALETTE["faint"],
    "forge.accent": f"bold {PALETTE['accent']}",
    "forge.added": PALETTE["added"],
    "forge.removed": PALETTE["removed"],
    "forge.failed": PALETTE["failed"],
    "forge.warn": PALETTE["warn"],
}

_console: Any = None


def console() -> Any:
    """The one shared console, or None when Rich is unavailable.

    `soft_wrap=False` so Rich wraps to the terminal instead of letting the
    terminal break lines wherever it likes — wrapping is a layout decision and
    the layout engine should be the one making it.
    """
    global _console
    if not AVAILABLE:
        return None
    if _console is None:
        # safe_box=False keeps the rounded corners. Rich substitutes square
        # ones whenever it suspects a legacy Windows console, which is a
        # reasonable default and wrong here: ansi.enable() has already put
        # the console into virtual-terminal mode, and the substitution was
        # firing on terminals that render ╭ perfectly well.
        _console = Console(theme=Theme(_THEME), soft_wrap=False,
                           highlight=False, safe_box=False)
    return _console


def width() -> int:
    c = console()
    return c.width if c is not None else ansi.terminal_width()


def render(renderable: Any) -> str:
    """A Rich renderable as a plain string, ready for `ansi.write`.

    Returning text rather than printing keeps every component pure: the
    caller decides when and where output happens, the functions stay
    testable without a terminal, and `banner()` keeps the contract it has
    always had — a string in, a string out.
    """
    c = console()
    if c is None:
        return ""
    with c.capture() as captured:
        c.print(renderable)
    # Rich pads every cell to its column width. Those trailing spaces are
    # invisible in the terminal and turn up the moment a transcript is pasted
    # somewhere, or a line wraps and drags a run of blanks onto the next row.
    return "\n".join(line.rstrip()
                     for line in captured.get().rstrip("\n").split("\n"))


# ── Components ───────────────────────────────────────────────────────────────


def _section(title: str, rows: list[tuple[str, str]]) -> Any:
    """A titled two-column block: a key and what it is."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="forge.fg", no_wrap=True)
    grid.add_column(style="forge.faint", overflow="ellipsis", no_wrap=True)
    for key, what in rows:
        grid.add_row(key, what)
    return Group(Text(title, style="forge.accent"), grid)


def welcome(agent: str, model: str, workspace: str, tools: int,
            tips: tuple[tuple[str, str], ...],
            facts: list[tuple[str, str]] | None = None,
            resume: list[tuple[str, str]] | None = None) -> str:
    """The opening frame, or "" when Rich is unavailable.

    Two columns, and the right one carries INFORMATION rather than decoration.
    An opening screen that only restates what was typed to launch it is empty
    space the operator has to scroll past; the things actually worth knowing
    before the first prompt are what can be picked back up, what state the
    repository is in, and what the agent has loaded. All of it is already known
    at this point and none of it is visible anywhere else without asking.
    """
    c = console()
    if c is None:
        return ""

    left: list[Any] = [
        Group(Text("▲ FORGE", style="forge.accent"),
              Text(agent, style="forge.muted")),
        Text(""),
    ]
    left.append(_section("Session", [("model", model), ("tools", str(tools))]
                         + list(facts or [])))
    left.append(Text(""))
    left.append(Text(workspace, style="forge.faint",
                     overflow="ellipsis", no_wrap=True))

    right: list[Any] = []
    if resume:
        right.append(_section("Resume", resume))
        right.append(Text(""))
    if tips:
        right.append(_section("Getting started", list(tips)))

    # Explicit widths, not ratios. A grid with `expand` inside a fixed-width
    # Panel has no idea what it is allowed to occupy and collapses every cell
    # to an ellipsis — the columns have to be told.
    outer = min(c.width - 2, 88)
    inner = outer - 6                      # borders + horizontal padding
    gap = 3
    left_w = max(24, (inner - gap) * 4 // 9)
    right_w = max(20, inner - gap - left_w)

    columns = Table.grid(padding=(0, gap))
    columns.add_column(width=left_w, overflow="ellipsis")
    columns.add_column(width=right_w, overflow="ellipsis")
    columns.add_row(Group(*left), Group(*right) if right else Text(""))

    return render(Padding(
        Panel(columns, border_style="forge.faint", box=ROUNDED,
              padding=(1, 2), width=outer),
        (0, 0, 0, 1)))


def tool_call(label: str, target: str) -> str:
    """`● Read(calc.py)` — the verb and its object as one token."""
    c = console()
    if c is None:
        return ""
    line = Text()
    line.append("● ", style=PALETTE["accent"])
    line.append(label, style="bold")
    if target:
        line.append(f"({target})", style="forge.muted")
    return render(line)


def tool_result(body: str, prefix: str = "", failed: bool = False) -> str:
    """The answer, indented under the call that produced it."""
    c = console()
    if c is None:
        return ""
    style = "forge.failed" if failed else "forge.muted"
    lines = body.splitlines() or [""]
    grid = Table.grid(padding=(0, 0))
    grid.add_column(width=5, no_wrap=True)
    grid.add_column(overflow="fold")
    first = (prefix + lines[0]) if prefix else lines[0]
    grid.add_row(Text("  └  ", style="forge.faint" if not failed else "forge.failed"),
                 Text(first, style=style))
    for line in lines[1:]:
        grid.add_row("", Text(line, style=style))
    return render(grid)


def diff(header: str, body: str) -> str:
    """A unified diff, coloured by sign and indented into the gutter."""
    c = console()
    if c is None:
        return ""
    grid = Table.grid(padding=(0, 0))
    grid.add_column(width=5, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(Text("  └  ", style="forge.faint"), Text(header, style="bold"))
    for line in body.splitlines():
        grid.add_row("", _diff_line(line))
    return render(grid)


def _diff_line(line: str) -> Any:
    if line.startswith("@@"):
        return Text(line, style="forge.faint")
    if line.startswith("+"):
        return Text(line, style="forge.added")
    if line.startswith("-"):
        return Text(line, style="forge.removed")
    if line.startswith("…"):
        return Text(line, style="forge.faint")
    return Text(line, style="forge.muted")


def code(source: str, lexer: str = "python") -> bool:
    """Syntax-highlighted source. Used where a block is shown whole rather than
    as a diff — a file the operator asked to see, a snippet in a result."""
    c = console()
    if c is None:
        return ""
    c.print(Syntax(source, lexer, theme="ansi_dark", background_color="default",
                   word_wrap=True))
    return True


def rule() -> bool:
    c = console()
    if c is None:
        return ""
    c.print(Text("─" * min(c.width, 80), style="forge.faint"))
    return True


def note(text: str, style: str = "forge.faint") -> bool:
    c = console()
    if c is None:
        return ""
    c.print(Text(text, style=style))
    return True
