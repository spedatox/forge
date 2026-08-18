"""Reading something longer than the screen, without losing the screen.

`/transcript` printed the whole conversation into scrollback. On a short session
that is fine and on a long one it is the worst possible answer: it buries the
conversation under a copy of itself, and the operator's own scrollback — the
thing they would have used to find what they were looking for — is now full of
the search results instead.

Codex solves this with `pager_overlay.rs`, a scrollable overlay inside its
viewport. Forge's TUI is inline and deliberately does not own the screen, so the
same answer is not available: there is no viewport to overlay onto. What is
available is the shape `less` has used for forty years — a screenful, a prompt,
and a key — and it costs no architecture at all.

**It hands the terminal back.** Every page is written normally and stays in
scrollback, so a pager session leaves behind exactly what the operator chose to
look at rather than everything. Quitting early is the common case and the point.

**It degrades to the old behaviour.** Without a tty, without key reading, or on
anything short enough to fit, this prints the text and returns — which is what
`/transcript` did before, so nothing that worked stops working.
"""
from __future__ import annotations

import asyncio

from forge.tui import ansi, keys

_FOOTER_ROWS = 2
"""Rows kept for the prompt. One for the prompt itself, one so the last line of
content is not flush against it — a page whose final line touches the prompt
reads as though it continues into it."""

MIN_PAGE = 5
"""Below this the pager is worse than printing. A two-line page means more
keystrokes than content, and the operator is better served by scrollback."""


async def page(text: str, *, title: str = "") -> None:
    """Show `text` a screenful at a time.

    Keys are the ones muscle memory already has from `less`: space or enter for
    the next page, q or ctrl+c to stop. Arrows work too, because the permission
    prompt taught them to this terminal and an operator who just learned ↓ there
    will try it here.
    """
    lines = text.splitlines() or [""]
    height = ansi.terminal_height()
    per_page = max(MIN_PAGE, height - _FOOTER_ROWS)

    # Short enough, or a terminal that cannot page: print it and be done. The
    # check is on the CONTENT, not on preference — paging something that fits is
    # an extra keystroke to see what was already visible.
    if len(lines) <= per_page or not keys.available():
        ansi.write(text)
        return

    if title:
        ansi.write(ansi.paint(f"  {title} — {len(lines)} lines", "dim"))

    shown = 0
    while shown < len(lines):
        chunk = lines[shown:shown + per_page]
        ansi.write("\n".join(chunk))
        shown += len(chunk)
        if shown >= len(lines):
            break

        remaining = len(lines) - shown
        prompt = ansi.paint(
            f"  ── {shown}/{len(lines)} lines · space to continue · "
            f"q to stop ({remaining} more) ──", "dim")
        # Written WITH its newline so the cursor sits on the row below it, which
        # is what `rewind(1)` expects. Written without one, the cursor is still
        # on the prompt row and a rewind of 1 lands a row too high — erasing the
        # last line of content the operator was reading.
        ansi.write(prompt)
        # Off the event loop: this blocks until a key arrives, and a pager is
        # reached from an async command handler. Blocking there would stall
        # everything else the loop is doing for as long as the operator reads.
        key = await asyncio.to_thread(keys.read_key)
        # The prompt is erased whatever happens next, so the page above it reads
        # as continuous text rather than being interrupted by a row of
        # navigation furniture every screenful.
        ansi.rewind(1)
        if key is None:                       # the terminal stopped talking
            ansi.write("\n".join(lines[shown:]))
            return
        if key in ("q", keys.CANCEL):
            ansi.write(ansi.paint(f"  ── stopped, {remaining} lines unread ──", "dim"))
            return
