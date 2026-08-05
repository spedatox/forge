"""Turning the engine's event stream into something a person can read.

The engine already emits everything the loop knows — chunk, tool, tool_result,
usage, compact, error. This subscribes to that same stream, which means the TUI
watches a job exactly the way Mark VI does over the socket. There is no second
path and no privileged access: if something is invisible here it is invisible to
Heartbreaker too, and that is a bug in the event vocabulary rather than in the
renderer.

The distinction the layout exists to preserve: what the model **said** is plain
text at the margin, and everything the harness **did** is indented and dim. A
transcript that blurs those reads like the model ran the commands itself.
"""
from __future__ import annotations

import json
from typing import Any

from forge.tui import ansi


class StreamRenderer:
    """Consumes JobEvents for one turn and writes the terminal view."""

    def __init__(self, verbose: bool = False, spinner: Any = None,
                 on_truncated: Any = None) -> None:
        self.verbose = verbose
        # Called with (tool, full_text) whenever a result was shortened, so
        # ctrl+o can put back what the one-line view cut. An inline renderer
        # cannot rewrite text that has already scrolled, so the honest
        # version of "expand" is to print it again in full.
        self.on_truncated = on_truncated
        # The live line, if one is running. Every write below clears it
        # first: the spinner owns a row it redraws in place, and prose
        # printed on top of a half-drawn frame is unreadable.
        self.spinner = spinner
        self._wrote_text = False        # has model prose landed this turn?
        self._last_was_harness = False  # was the last thing written a tool line?
        self._in_flight: set[str] = set()
        self._batch = 0                 # calls in the current parallel batch
        self._tool_names: dict[str, str] = {}
        self.saw_error = False          # so the turn summary does not repeat it

    async def __call__(self, event: Any) -> None:
        kind = getattr(event, "type", None) or event.get("type")
        data = getattr(event, "data", None) if hasattr(event, "type") else event.get("data")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            return
        if self.spinner is not None:
            self.spinner.clear()
        handler(data)

    # ── The model's own output ───────────────────────────────────────────────
    def _on_chunk(self, data: Any) -> None:
        text = str(data or "")
        if not text:
            return
        if self.spinner is not None:
            self.spinner.add_chars(len(text))
            self.spinner.set_status("Responding")
        if not self._wrote_text:
            ansi.write()
            self._wrote_text = True
        elif self._last_was_harness:
            # Coming back from a tool block: without this the model's next
            # sentence starts on the line under a gutter and the two read as
            # one paragraph written by the same voice.
            ansi.write()
        self._last_was_harness = False
        # No newline: deltas arrive mid-word and the point of streaming is that
        # it appears at the speed it is produced.
        ansi.write(text, end="")

    def _on_done(self, data: Any) -> None:
        if self._wrote_text:
            ansi.write()

    # ── What the harness did ─────────────────────────────────────────────────
    def _on_tool(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        name = str(data.get("name", "?"))
        self._tool_names[str(data.get("id"))] = name
        self._in_flight.add(str(data.get("id")))
        self._batch = max(self._batch, len(self._in_flight))
        if self.spinner is not None:
            self.spinner.set_status(f"Running {name}")
        args = data.get("input") or {}
        target = _summarize_args(args)
        ansi.write()
        # `⏺ Read(calc.py)` — verb and object in one token, at the margin. The
        # older `read_file  calc.py` said the same thing in more space and read
        # as two separate facts.
        ansi.write(ansi.paint("● ", "cyan")
                   + ansi.paint(_label(name), "bold")
                   + ansi.paint(f"({target})" if target else "", "grey"))
        self._last_was_harness = True

    def _on_tool_result(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        content = str(data.get("content", ""))
        failed = bool(data.get("is_error"))

        # An operator-facing rendering wins over the model-facing text: for an
        # edit that is the difference between "Edited calc.py (1 replacement)"
        # and seeing what actually landed in the file.
        call_id = str(data.get("tool_use_id"))
        # Tools run in parallel, so results arrive as a batch under a batch
        # of calls. With one in flight the pairing is obvious; with several
        # it is not, and an unlabelled answer belongs to no question.
        prefix = ""
        if self._batch > 1:
            prefix = _label(self._tool_names.get(call_id, "")) + "  "
        self._in_flight.discard(call_id)
        if not self._in_flight:
            self._batch = 0
        self._last_was_harness = True

        display = data.get("display")
        if display and not failed:
            self._write_display(str(display), prefix)
            return

        if self.verbose:
            body = content
        else:
            body = _preview(content)
            if body != content.strip() and self.on_truncated is not None:
                self.on_truncated(self._tool_names.get(call_id, "tool"), content)
        _write_gutter(prefix + body, failed=failed)

    def _write_display(self, display: str, prefix: str = "") -> None:
        """A tool's operator-facing rendering — today, a diff.

        Colour is applied here rather than in the tool, so the same text can go
        to a socket, a log, or a terminal with no colour at all."""
        lines = display.splitlines()
        if not lines:
            return
        _write_gutter(prefix + lines[0])
        for line in lines[1:]:
            ansi.write(_INDENT + _paint_diff_line(line))

    # ── Harness housekeeping the operator should still see ───────────────────
    def _on_compact(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        stage = data.get("stage")
        if self.spinner is not None:
            self.spinner.set_status("Compacting")
        if stage == "elide":
            note = f"reclaimed {data.get('freed_chars', 0):,} chars of old tool output"
        elif stage == "summarize":
            note = "summarizing the conversation so far…"
        else:
            note = f"compacted to {data.get('messages', '?')} messages"
        ansi.write(ansi.paint(f"  ◆ context: {note}", "magenta"))

    def _on_error(self, data: Any) -> None:
        ansi.write()
        ansi.write(ansi.paint(f"  ✗ {humanize_error(str(data))}", "red"))
        self.saw_error = True

    def _on_usage(self, data: Any) -> None:
        if self.verbose and isinstance(data, dict):
            ansi.write(ansi.paint(
                f"  · {data.get('prompt', 0):,} in / {data.get('output', 0):,} out"
                f"  ({int(100 * data.get('fullness', 0))}% full)", "dim"))


# The transcript is two columns: what happened at the margin, what came back
# beneath it. `⎿` opens the second column and everything after it lines up
# under that opening, so a result is visibly subordinate to the call that
# produced it rather than another event in the same list.
GUTTER = "  └  "          # two spaces, glyph, two spaces
_INDENT = " " * 5


def _write_gutter(text: str, failed: bool = False) -> None:
    """Write a result under its call, wrapped into the gutter."""
    lines = text.splitlines() or [""]
    style = "red" if failed else "grey"
    head = ansi.paint(GUTTER, "red" if failed else "dim")
    ansi.write(head + ansi.paint(lines[0], style))
    for line in lines[1:]:
        ansi.write(_INDENT + ansi.paint(line, style))


RESULT_LINES = 4
"""How much of a result is shown before it is cut.

Enough for a shell result — exit code, a line of stdout, a line of stderr —
and not enough for a grep to bury the conversation it is part of. ctrl+o
puts back whatever this cut."""


def _preview(content: str) -> str:
    """The readable head of a tool result, keeping its LINES.

    Flattening newlines turned a shell result into `exit_code: 0 ⏎ stdout: ⏎
    5`, which is illegible at exactly the moment the operator is checking
    whether a command worked. Structure is most of what makes a result
    scannable, so it is kept and the tail is dropped instead."""
    width = max(20, ansi.terminal_width() - 8)
    lines = [ln.rstrip() for ln in content.strip().splitlines()]
    lines = [ln for ln in lines if ln.strip()] or [""]
    head = [ansi.truncate(ln, width) for ln in lines[:RESULT_LINES]]
    if len(lines) > RESULT_LINES:
        head.append(f"… +{len(lines) - RESULT_LINES} lines  (ctrl+o)")
    return "\n".join(head)


def _label(wire_name: str) -> str:
    """`read_file` → `Read`. The argument already says what is being read,
    so the noun in the tool name is repetition."""
    parts = wire_name.split("_")
    if len(parts) > 1 and parts[-1] in ("file", "command"):
        parts = parts[:-1]
    return "".join(p.capitalize() for p in parts)


def _summarize_args(args: dict[str, Any]) -> str:
    """The one detail that says what a call will actually do.

    A tool row is only useful if it names the target — `run_command` tells you
    nothing, `run_command  pytest -q` tells you everything. The argument that
    matters is nearly always the first of command/path/pattern."""
    for key in ("command", "path", "pattern", "url", "name", "question"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return ansi.truncate(value, max(20, ansi.terminal_width() - 24))
    # No recognisable target: show nothing. A dump of the raw arguments is
    # unreadable at a glance and costs the width the tool name needs.
    return ""


# Provider errors arrive as a raw JSON blob wrapped in an SDK exception name.
# That is the right thing to log and the wrong thing to show someone who just
# wants to know what to do about it. Each entry is (marker in the raw text, what
# the operator can act on).
_KNOWN_ERRORS = (
    ("credit balance is too low",
     "Your Anthropic account is out of credit. Top it up at "
     "console.anthropic.com → Plans & Billing."),
    ("invalid x-api-key",
     "That API key was rejected. Check ANTHROPIC_API_KEY in your .env."),
    ("authentication_error",
     "The provider rejected the credentials. Check the key in your .env."),
    ("permission_error",
     "That key is not allowed to use this model."),
    ("not_found_error",
     "The provider does not know that model — check the profile's model ref."),
    ("rate_limit",
     "Rate limited. Forge retries these automatically; this one outlasted the "
     "retry budget."),
    ("overloaded",
     "The provider is overloaded. Forge retries these automatically; this one "
     "outlasted the retry budget."),
    ("prompt is too long",
     "The conversation outgrew the window and could not be compacted further. "
     "Try /clear."),
)


def humanize_error(raw: str) -> str:
    """Turn a provider's error into something with a next step in it.

    The raw text is kept as a second line rather than discarded: the friendly
    sentence is a guess about intent, and when the guess is wrong the operator
    still needs what actually came back."""
    lowered = raw.lower()
    for marker, advice in _KNOWN_ERRORS:
        if marker in lowered:
            return f"{advice}\n    {ansi.truncate(raw, 160)}"
    return ansi.truncate(raw, 300)


# Shown once, under the banner. Not a feature list — the three things a person
# cannot guess and will otherwise never discover, because nothing in a bare
# prompt hints that they exist. `/help` covers the rest.
_TIPS = (
    ("!cmd", "shell, no model turn"),
    ("@file", "complete a path"),
    ("/", "commands"),
    ("shift+tab", "act / plan"),
)


def _box(rows: list[str], width: int) -> list[str]:
    """A framed block. Rows are padded on their VISIBLE width — a painted
    string is longer than it looks, so measuring the raw one puts the right
    edge somewhere different on every line."""
    out = [ansi.paint("╭" + "─" * (width - 2) + "╮", "dim")]
    for row in rows:
        pad = " " * max(0, width - 4 - ansi.visible_width(row))
        out.append(ansi.paint("│", "dim") + " " + row + pad + " "
                   + ansi.paint("│", "dim"))
    out.append(ansi.paint("╰" + "─" * (width - 2) + "╯", "dim"))
    return out


def banner(agent: str, model: str, workspace: str, tools: int,
           tips: bool = True) -> str:
    """The opening frame: what is running, where, and how to begin.

    A frame rather than loose lines, because the first screen has to read as
    one object. Loose lines under a heading look like output that has already
    started, which is exactly what an operator should not think before they
    have typed anything.
    """
    width = max(46, min(ansi.terminal_width() - 2, 78))
    inner = width - 4

    rows = [
        ansi.paint("▲ FORGE", "bold", "cyan") + ansi.paint(f"   {agent}", "grey"),
        "",
        ansi.paint(f"{model}", "dim"),
        ansi.paint(ansi.truncate(workspace, inner), "dim"),
    ]
    lines = [""] + _box(rows, width)

    if tips:
        # Two per row: four one-line hints stacked vertically read as a menu,
        # which invites reading them all before starting. Paired, they read as
        # a footnote, which is what they are.
        lines.append("")
        pairs = [_TIPS[i:i + 2] for i in range(0, len(_TIPS), 2)]
        for pair in pairs:
            cells = [ansi.paint(f"{key:<10}", "cyan") + ansi.paint(f"{what:<24}", "dim")
                     for key, what in pair]
            lines.append("  " + "".join(cells).rstrip())
    lines.append("")
    lines.append(ansi.paint(f"  {tools} tools", "dim")
                 + ansi.paint("  ·  ", "dim")
                 + ansi.paint("/help", "cyan") + ansi.paint(" for commands", "dim"))
    lines.append("")
    return "\n".join(lines)


def _paint_diff_line(line: str) -> str:
    """Green added, red removed, dim context — the whole reason a diff is
    readable at a glance. Applied at the edge so `warden/diff.py` stays plain
    text that any surface can render its own way."""
    if line.startswith("@@"):
        return ansi.paint(line, "cyan")
    if line.startswith("+"):
        return ansi.paint(line, "green")
    if line.startswith("-"):
        return ansi.paint(line, "red")
    if line.startswith("…"):
        return ansi.paint(line, "dim")
    return ansi.paint(line, "grey")
