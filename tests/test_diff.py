"""Rendering an edit as a diff.

`Edited calc.py (1 replacement)` says something happened and nothing about what.
Reviewing then means opening the file and reconstructing the change by eye,
which is the point at which most people stop reading and start trusting — and
trusting an agent's self-report is the failure this system keeps running into.

The constraint that shapes the module is that this text has two audiences with
opposite needs. The model wrote the change and needs "it applied"; the operator
needs to see it. So the diff rides `ToolResult.display`, which never reaches the
model — meaning the transcript and the token bill do not carry it.
"""
from __future__ import annotations

from forge.warden.diff import MAX_DIFF_LINES, render_edit, summarize, unified
from forge.warden.tool import ToolResult

BEFORE = "def add(a, b):\n    return a - b\n"
AFTER = "def add(a, b):\n    return a + b\n"


# ── The diff ────────────────────────────────────────────────────────────────


def test_a_change_shows_both_sides():
    out = unified(BEFORE, AFTER)
    assert "-    return a - b" in out
    assert "+    return a + b" in out


def test_unchanged_content_produces_nothing():
    assert unified(BEFORE, BEFORE) == ""


def test_context_is_bounded_not_the_whole_file():
    """An edit to line 400 of a 900-line file should print a handful of lines."""
    old = "\n".join(f"line {i}" for i in range(400))
    new = old.replace("line 200", "line 200 CHANGED")

    out = unified(old, new)

    assert "line 200 CHANGED" in out
    assert len(out.splitlines()) < 20
    assert "line 5" not in out


def test_a_hunk_header_locates_the_change():
    assert unified(BEFORE, AFTER).startswith("@@")


def test_a_huge_diff_is_truncated_with_a_pointer():
    """Past a point the operator wants `git diff`, and saying so beats printing
    four hundred lines they will skip."""
    old = "\n".join(f"x{i}" for i in range(500))
    new = "\n".join(f"y{i}" for i in range(500))

    out = unified(old, new)
    lines = out.splitlines()

    assert len(lines) <= MAX_DIFF_LINES + 1
    assert "git diff" in lines[-1]


def test_creation_diffs_against_nothing():
    out = unified("", "hello\n")
    assert "+hello" in out


# ── The summary ─────────────────────────────────────────────────────────────


def test_summary_counts_both_directions():
    assert summarize(BEFORE, AFTER) == "+1 -1"
    assert summarize("a\n", "a\nb\n") == "+1"
    assert summarize("a\nb\n", "a\n") == "-1"


def test_summary_of_no_change_says_so():
    assert summarize(BEFORE, BEFORE) == "no change"


def test_render_edit_leads_with_path_and_shape():
    out = render_edit("calc.py", BEFORE, AFTER)
    assert out.splitlines()[0] == "calc.py  +1 -1"


def test_render_edit_on_no_change_is_one_line():
    assert render_edit("calc.py", BEFORE, BEFORE) == "calc.py: no change"


# ── The split that makes it free ────────────────────────────────────────────


def test_display_defaults_to_absent():
    """Existing tools keep their exact model-facing behaviour."""
    assert ToolResult("done").display is None


def test_display_is_separate_from_the_model_facing_content():
    """The diff must not ride in `content` — that would pay for it in context on
    every later turn to tell the model what it already knows."""
    result = ToolResult("Edited calc.py (1 replacement).",
                        display=render_edit("calc.py", BEFORE, AFTER))

    assert "return a + b" not in result.content
    assert "return a + b" in result.display


def test_the_pure_diff_carries_no_colour():
    """Colour is added at the edge so the same text can go to a log or a socket."""
    assert "\x1b[" not in unified(BEFORE, AFTER)
    assert "\x1b[" not in render_edit("calc.py", BEFORE, AFTER)
