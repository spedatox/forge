"""Typing while the agent works.

The gap this closes: Forge's prompt existed only between turns, so an agent
heading the wrong way at iteration two left the operator two options — sit
through forty more iterations, or ctrl+c and lose the turn. Both references
treat mid-run input as core (Codex keeps its composer live under a running task;
DSH ships `steer`/`followup`/`inject`), and it is the one UI gap that changes how
the tool feels rather than how it looks.

What is protected here is mostly about WHERE input lands, not that it lands.
A message delivered at the wrong point corrupts a transcript — between an
assistant turn and its tool results is the shape the API rejects, and a separate
user message breaks the strict alternation compaction depends on.
"""
from __future__ import annotations

import asyncio

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.inbox import Inbox, render
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine

from pydantic import BaseModel


class _Args(BaseModel):
    pass


class Ping(Tool):
    name = "ping"
    description = "does nothing"
    Args = _Args
    READ_ONLY = True

    async def call(self, args, ctx):
        return ToolResult("pong")


def _ctx(tmp_path):
    from forge.cell.base import CellPolicy
    from forge.cell.subprocess_cell import SubprocessCell

    cell = SubprocessCell(workspace=tmp_path, policy=CellPolicy())
    return ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _texts(messages) -> list[str]:
    """Every text block in the transcript, flattened."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(b.get("text", "") for b in content if b.get("type") == "text")
    return out


# ── the queue ────────────────────────────────────────────────────────────────


def test_blank_input_is_dropped_rather_than_queued():
    """An empty line is how a person clears the composer, not something to say."""
    box = Inbox()
    box.push("   ")
    box.push("")
    assert not box


def test_claiming_empties_the_queue():
    box = Inbox()
    box.push("one")
    assert box.claim() == ["one"]
    assert box.claim() == []


def test_peek_does_not_consume():
    """Used to hand unclaimed input back when a turn ends; taking it there would
    race the loop's own claim."""
    box = Inbox()
    box.push("one")
    assert box.peek() == ["one"]
    assert len(box) == 1


# ── where it lands in the transcript ─────────────────────────────────────────


def test_input_rides_with_the_tool_results(tmp_path):
    """Not as its own message. A separate user message would break the strict
    user/assistant alternation `find_cut` and `rebuild` depend on, and the
    tool_result block must stay adjacent to the assistant turn that requested
    it."""
    box = Inbox()
    box.push("actually use the other file")

    steps = [
        lambda m: ("working", [tool_call("ping")]),
        lambda m: ("done", []),
    ]
    warden = Warden(system_prompt="", tools={"ping": Ping()},
                    model=ScriptedModel(steps), ctx=_ctx(tmp_path), inbox=box)
    term = asyncio.run(warden.run("go"))

    assert term.reason is StopReason.COMPLETED
    # The message carrying tool_results must ALSO carry the interjection.
    carriers = [m for m in term.messages
                if isinstance(m.get("content"), list)
                and any(b.get("type") == "tool_result" for b in m["content"])]
    assert carriers, "no tool_result message at all"
    joined = " ".join(b.get("text", "") for m in carriers for b in m["content"]
                      if b.get("type") == "text")
    assert "actually use the other file" in joined


def test_input_arriving_as_the_turn_ends_continues_it(tmp_path):
    """The case that matters most: you type "also update the README" while it is
    writing its summary. Ending the turn and making the operator start another
    to say the same thing is the behaviour this replaces."""
    box = Inbox()
    seen = {"turns": 0}

    def _step(_m):
        seen["turns"] += 1
        if seen["turns"] == 1:
            box.push("also update the README")   # arrives while turn 1 finishes
        return ("all done", [])

    # Both steps count, or the assertion below reads 1 whether it worked or not.
    warden = Warden(system_prompt="", tools={},
                    model=ScriptedModel([_step, _step]),
                    ctx=_ctx(tmp_path), inbox=box)
    term = asyncio.run(warden.run("go"))

    assert seen["turns"] == 2, "the turn ended instead of picking the message up"
    assert any("also update the README" in t for t in _texts(term.messages))


def test_no_inbox_means_no_behaviour_change(tmp_path):
    """Headless paths pass none. The loop must not grow a step for an inbox
    that can never be filled."""
    warden = Warden(system_prompt="", tools={},
                    model=ScriptedModel([lambda m: ("done", [])]),
                    ctx=_ctx(tmp_path))
    term = asyncio.run(warden.run("go"))
    assert term.reason is StopReason.COMPLETED


def test_nothing_is_claimed_twice(tmp_path):
    box = Inbox()
    box.push("once")

    steps = [
        lambda m: ("a", [tool_call("ping")]),
        lambda m: ("b", [tool_call("ping")]),
        lambda m: ("done", []),
    ]
    warden = Warden(system_prompt="", tools={"ping": Ping()},
                    model=ScriptedModel(steps), ctx=_ctx(tmp_path), inbox=box)
    term = asyncio.run(warden.run("go"))

    occurrences = sum(t.count("once") for t in _texts(term.messages))
    assert occurrences == 1, f"delivered {occurrences} times"


# ── how it is presented to the model ─────────────────────────────────────────


def test_the_interjection_says_it_arrived_mid_run():
    """Unmarked, it reads as part of the original task and the model tries to
    reconcile it with instructions given before anything had happened."""
    text = render(["use the other file"])
    assert "while you were working" in text
    assert "use the other file" in text


def test_the_interjection_outranks_the_original_instruction():
    """A person who interrupts a running job is not offering a suggestion. A
    model weighing it equally against the original prompt averages the two and
    satisfies neither."""
    text = render(["stop refactoring, just fix the bug"])
    assert "takes precedence" in text
    assert "do not ask whether to apply it" in text


def test_several_messages_are_delivered_together():
    text = render(["first", "second"])
    assert "first" in text and "second" in text
    assert "these" in text and "messages" in text


# ── the composer ─────────────────────────────────────────────────────────────


def test_the_composer_hides_itself_until_something_is_typed():
    """An always-present empty input line under a running turn reads as a
    prompt waiting for an answer, and an operator who thinks the agent is
    blocked on them stops watching it work."""
    from forge.tui.composer import Composer

    c = Composer(Inbox())
    c._enabled = True                       # noqa: SLF001 — no tty under pytest
    assert c.line() is None
    c._feed("h")                            # noqa: SLF001
    assert c.line() is not None


def test_enter_queues_the_draft_and_clears_it():
    from forge.tui.composer import Composer
    from forge.tui import keys

    box = Inbox()
    c = Composer(box)
    c._enabled = True                       # noqa: SLF001
    for ch in "hello":
        c._feed(ch)                         # noqa: SLF001
    c._feed(keys.ENTER)                     # noqa: SLF001

    assert box.peek() == ["hello"]
    assert c.draft == ""


def test_capitals_survive_the_composer():
    """`read_key` lowercases for the permission prompt, where `Y` and `y` are
    the same answer. Reusing it here would make capitals impossible to type and
    the loss is unrecoverable by the caller."""
    from forge.tui.composer import Composer

    c = Composer(Inbox())
    c._enabled = True                       # noqa: SLF001
    for ch in "README":
        c._feed(ch)                         # noqa: SLF001
    assert c.draft == "README"


def test_ctrl_c_clears_a_draft_before_it_aborts():
    """Raw mode swallows SIGINT, so this is the interrupt path while the
    composer polls. Two-stage like a shell: ctrl+c after a typo costs the typo,
    not the turn."""
    from forge.tui.composer import Composer
    from forge.tui import keys

    aborted = []
    c = Composer(Inbox(), on_abort=lambda: aborted.append(1))
    c._enabled = True                       # noqa: SLF001
    c._feed("x")                            # noqa: SLF001

    c._feed(keys.CANCEL)                    # noqa: SLF001 — clears the draft
    assert c.draft == "" and not aborted

    c._feed(keys.CANCEL)                    # noqa: SLF001 — now it aborts
    assert aborted == [1]


def test_backspace_edits_the_draft():
    from forge.tui.composer import Composer

    c = Composer(Inbox())
    c._enabled = True                       # noqa: SLF001
    for ch in "abc":
        c._feed(ch)                         # noqa: SLF001
    c._feed("\x7f")                         # noqa: SLF001
    assert c.draft == "ab"


def test_arrow_keys_do_not_land_in_the_draft():
    from forge.tui.composer import Composer
    from forge.tui import keys

    c = Composer(Inbox())
    c._enabled = True                       # noqa: SLF001
    c._feed(keys.UP)                        # noqa: SLF001
    c._feed(keys.DOWN)                      # noqa: SLF001
    assert c.draft == ""


def test_a_terminal_that_cannot_give_keys_has_no_composer():
    """Degrades to exactly the behaviour that existed before: the turn runs,
    there is simply no input line."""
    from forge.tui.composer import Composer

    c = Composer(Inbox())
    c._enabled = False                      # noqa: SLF001
    assert c.line() is None
    c.start()                               # a no-op, not a crash
    assert asyncio.run(c.stop()) == ""
