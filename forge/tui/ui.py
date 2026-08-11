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
    # The band behind the operator's own message. Dark enough to read as a
    # surface rather than a highlight, light enough to be visible on the
    # near-black a terminal usually is.
    "user_bg": "grey19",
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
    "forge.user": f"{PALETTE['accent']} on {PALETTE['user_bg']}",
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
        # Colour follows ansi's decision rather than Rich's own detection.
        # They answer the same question separately and can disagree — on
        # Windows ansi.enable() switches virtual-terminal processing on, which
        # Rich has no way to know about — and the visible result is a
        # transcript where the harness lines are coloured and the components
        # are not. One decision, one place.
        # 256 colours, not Rich's own detection. On Windows it settles on the
        # legacy 16-colour system, which collapses the whole grey palette:
        # `grey19` becomes plain black, so the band behind a message is the
        # same colour as the terminal and simply is not there. ansi.enable()
        # has already switched virtual-terminal processing on, which is what
        # makes 256 available — Rich has no way to know that happened.
        _console = Console(theme=Theme(_THEME), soft_wrap=False,
                           highlight=False, safe_box=False,
                           color_system="256" if ansi._ENABLED else None,  # noqa: SLF001
                           force_terminal=True if ansi._ENABLED else None)  # noqa: SLF001
    return _console


def reset() -> None:
    """Drop the cached console so the next call re-reads the colour decision.

    Needed because `ansi.enable()` runs after import: a console built before it
    would have settled on the wrong answer and kept it for the session."""
    global _console
    _console = None


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

    # One column, Claude Code's shape. The previous version was two columns of
    # labelled fields, and at any real terminal width the labels themselves got
    # ellipsed — `cont…`, `bran…`, a model ref cut mid-word. A welcome screen
    # that truncates its own field names is worse than one that says less, and
    # it was saying less anyway: nothing in it could be read at a glance.
    #
    # So the box carries only what cannot be found elsewhere in one keystroke,
    # left-aligned, nothing clipped. Everything else moved to `/status`, which
    # is where a person goes when they actually want it.
    body: list[Any] = [
        Text.assemble(("✻ ", "forge.muted"),
                      ("Welcome to Forge", "forge.accent"),
                      (f"  ·  {agent}", "forge.faint")),
        Text(""),
        # No manual indent: the Panel's own padding already provides it, and
        # adding a second one is what made the previous version look inset from
        # its own border.
        Text.assemble(("/help", "forge.fg"),
                      (" for commands, ", "forge.faint"),
                      ("/status", "forge.fg"),
                      (" for this session", "forge.faint")),
        Text(""),
        Text(f"cwd: {workspace}", style="forge.faint", overflow="fold"),
        Text(f"{model}  ·  {tools} tools", style="forge.faint", overflow="fold"),
    ]

    outer = min(c.width - 2, 88)
    return render(Padding(
        Panel(Group(*body), border_style="forge.faint", box=ROUNDED,
              padding=(1, 2), width=outer),
        (0, 0, 0, 1)))


def welcome_footer(tips: tuple[tuple[str, str], ...],
                   resume: list[tuple[str, str]] | None = None) -> str:
    """What sits under the box: the undiscoverable keys, and what to resume.

    Outside the panel deliberately. Claude Code puts its hints under the box
    rather than inside it, and the reason holds here — inside, they are part of
    a frame the eye reads once and then skips forever; outside, they sit in the
    same place the transcript will occupy and read as the first line of it.
    """
    c = console()
    if c is None:
        return ""
    rows: list[Any] = []
    # One line each, never wrapped. A resume entry that spills onto a second
    # line leaves its timestamp stranded on a row of its own, which reads as a
    # separate entry — the list stops being countable at a glance, which is the
    # only thing a list like this is for.
    budget = max(20, min(c.width, 90) - 18)
    if resume:
        for key, what in resume:
            rows.append(Text.assemble(("  " + key.ljust(13), "forge.fg"),
                                      (_clip(what, budget), "forge.faint")))
        rows.append(Text(""))
    for key, what in tips:
        rows.append(Text.assemble(("  " + key.ljust(13), "forge.fg"),
                                  (what, "forge.faint")))
    return render(Group(*rows)) if rows else ""


def prompt_width() -> int:
    c = console()
    # Full terminal width, minus one column for safety at the right edge.
    # The old 100-char cap made the input frame look broken on wide terminals.
    return max(20, c.width - 1) if c is not None else 80


def prompt_top() -> str:
    """The top edge of the input frame, drawn above the line editor.

    Only the top and the lead-in, never a closing edge. prompt_toolkit owns the
    row the cursor is on and everything below it — a bottom border would be
    drawn before the input existed and then scrolled away by the first wrapped
    line. Two sides of a box is the most that can be honestly drawn here, and
    it still does the one thing a rule cannot: mark where input begins as a
    shape rather than a position.
    """
    c = console()
    if c is None:
        return ""
    w = prompt_width()
    return render(Text("╭" + "─" * (w - 2) + "╮", style="forge.faint")).rstrip("\n")


def prompt_lead() -> str:
    """The `│ > ` that opens the input row."""
    c = console()
    if c is None:
        return ""
    return render(Text.assemble(("│ ", "forge.faint"),
                                ("> ", "forge.fg"))).rstrip("\n")


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)] + "…"


PROSE_WIDTH = 96
"""Where the model's own text wraps.

Not the terminal width. A line running the full span of a wide window is
measurably harder to read — the eye loses its place on the return sweep — and
the reference TUI wraps for the same reason rather than filling the screen.
"""


def markdown(text: str) -> str:
    """The model's reply, rendered.

    Models write markdown whether or not anything renders it, so a raw
    transcript shows `- **The Forge** is` and the asterisks are noise the
    reader has to strip mentally on every line. Rendering costs one thing —
    the text can no longer appear token by token, because a heading or a fenced
    block cannot be laid out until it closes — which is why the caller buffers
    a reply and flushes it here when the segment ends.
    """
    c = console()
    if c is None:
        return ""
    body = text.strip()
    if not body:
        return ""
    try:
        from rich.markdown import Markdown

        rendered = Markdown(body, code_theme="ansi_dark",
                            inline_code_theme="ansi_dark")
    except Exception:  # noqa: BLE001 — malformed markdown must still be readable
        return render(Padding(Text(body, style="forge.fg"), (0, 0, 0, 0)))
    with c.capture() as captured:
        c.print(rendered, width=min(c.width, PROSE_WIDTH))
    return "\n".join(line.rstrip()
                     for line in captured.get().rstrip("\n").split("\n"))


def user_message(text: str, marker: str = "› ") -> str:
    """The operator's own words, on a band that runs the width of the terminal.

    Without it a question and its answer are two paragraphs of identical text
    and the eye has to parse them to tell which is which. A filled row is read
    before it is read — the shape alone says "this is where you spoke", which
    is what makes a long transcript skimmable.

    Full width rather than just behind the characters: a band that stops where
    the sentence stops looks like a highlight over the words, and the thing
    being marked is the turn, not the phrase.
    """
    c = console()
    if c is None:
        return ""
    body = " ".join(text.split())
    if not body:
        return ""

    import textwrap

    total = c.width
    # Wrapped by hand and each row padded to the full width. Rich will not
    # expand a styled Text on its own, so without this the fill stops where the
    # sentence stops and reads as a highlight over the words rather than as a
    # band marking the turn.
    rows = textwrap.wrap(f"{marker}{body}", width=max(10, total - 2),
                         subsequent_indent="  ") or [marker]
    out = []
    for row in rows:
        out.append(render(Text(" " + row.ljust(total - 1), style="forge.user")))
    return "\n".join(out)


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


def live_row(label: str, target: str, elapsed: float, *, last: bool = False,
             subagent: bool = False) -> str:
    """One in-flight row of the live region.

        ├─ ● Grep(retry_attempt)                            4s
        └─ ◆ verify                       running the suite  31s

    Claude Code's shape exactly — `├─`/`└─` connectors, the `●` bullet, the
    `Name(target)` token — carried in Forge's greys rather than its colours.
    The whole row is structure, and structure is grey here: nothing in it has
    happened yet, so nothing in it has earned an accent. Colour stays reserved
    for the two things that mean something, which are what changed and what
    failed, and both of those are reported in scrollback once the call returns.

    The elapsed time is right-aligned into its own column so a batch reads as a
    column of durations rather than ten ragged lines. That column is the reason
    the region exists: with ten calls in flight, the only question worth asking
    at a glance is which one is not coming back.
    """
    c = console()
    if c is None:
        return ""
    width = max(30, c.width)
    stamp = f"{elapsed:.0f}s"

    grid = Table.grid(padding=(0, 0))
    grid.add_column(width=5, no_wrap=True)                    # "  ├─ "
    grid.add_column(overflow="ellipsis", ratio=1)             # the call
    grid.add_column(width=len(stamp) + 2, justify="right", no_wrap=True)
    grid.width = width

    connector = Text("  " + ("└─ " if last else "├─ "), style="forge.faint")
    body = Text()
    # A subagent is a loop of its own, not a call — the diamond is the same
    # glyph `render.py` already uses for harness housekeeping, so the operator
    # does not have to learn a second vocabulary for "this is not a tool".
    body.append("◆ " if subagent else "● ", style="forge.muted")
    body.append(label, style="forge.fg")
    if target:
        body.append(f"({target})" if not subagent else f"  {target}",
                    style="forge.faint")
    grid.add_row(connector, body, Text(stamp, style="forge.faint"))
    return render(grid)


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
