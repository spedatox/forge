"""todo_write and the plan's survival through compaction.

The failure this exists to stop is quiet: a long job's plan lives only in the
transcript, compaction replaces the turns that held it, and the agent comes out
the other side having forgotten steps five and six. It does not error. It
finishes step four and reports success.

So the tests that matter are not "can it store a list" — they are "is the list
still there after the transcript it was written into has been thrown away".
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tools import ALL_TOOLS, CODING_TOOLS
from forge.tools.todo import TodoItem, TodoWrite, TodoWriteArgs
from forge.warden.todos import COMPLETED, IN_PROGRESS, MAX_ITEMS, PENDING, Todo, TodoList
from forge.warden.tool import ToolContext


def _ctx(todos: TodoList | None = None) -> ToolContext:
    return ToolContext(
        agent_id="optimus", cell=None, graph=None, files=None,
        permissions=None, network_allowed=False,
        todos=todos if todos is not None else TodoList(),
    )


def _write(items, ctx=None):
    ctx = ctx or _ctx()
    args = TodoWriteArgs(todos=[TodoItem(content=c, status=s) for c, s in items])
    return asyncio.run(TodoWrite().call(args, ctx)), ctx


# ── Wiring ──────────────────────────────────────────────────────────────────


def test_todo_write_is_registered_and_in_the_coding_group():
    assert "todo_write" in ALL_TOOLS
    assert TodoWrite in CODING_TOOLS


def test_it_is_harmless_enough_to_call_freely():
    """No file, no command — nothing for plan mode or the gate to weigh."""
    tool = TodoWrite()
    args = TodoWriteArgs(todos=[])
    assert tool.is_destructive(args) is False
    # Two writes racing would silently lose one, so it is not parallel-safe.
    assert tool.is_concurrency_safe(args) is False


# ── Writing a plan ──────────────────────────────────────────────────────────


def test_a_plan_is_stored_and_rendered_back():
    result, ctx = _write([("read the failing test", COMPLETED),
                          ("fix the parser", IN_PROGRESS),
                          ("run the suite", PENDING)])

    assert not result.is_error
    assert len(ctx.todos) == 3
    assert "[x] read the failing test" in result.content
    assert "[~] fix the parser" in result.content
    assert "[ ] run the suite" in result.content
    assert "(1/3 done)" in result.content


def test_the_list_is_replaced_not_merged():
    """Whole-list semantics: no item-id bookkeeping to desync."""
    _, ctx = _write([("a", PENDING), ("b", PENDING)])
    _write([("c", PENDING)], ctx)

    assert [t.content for t in ctx.todos.items] == ["c"]


def test_finishing_everything_is_called_out():
    result, _ = _write([("a", COMPLETED), ("b", COMPLETED)])
    assert "All 2 steps complete" in result.content


# ── The rules, enforced rather than requested ───────────────────────────────


def test_two_things_in_progress_is_refused():
    """A list with four things in flight has stopped tracking anything."""
    result, ctx = _write([("a", IN_PROGRESS), ("b", IN_PROGRESS)])

    assert result.is_error
    assert "Exactly one step is current" in result.content
    assert len(ctx.todos) == 0, "a refused write must not partially apply"


def test_an_unknown_status_is_refused_with_the_valid_set():
    result, _ = _write([("a", "doing")])
    assert result.is_error and "pending" in result.content


def test_an_empty_item_is_refused():
    result, _ = _write([("   ", PENDING)])
    assert result.is_error and "empty content" in result.content


def test_an_absurdly_long_list_is_refused():
    result, _ = _write([(f"step {i}", PENDING) for i in range(MAX_ITEMS + 1)])
    assert result.is_error and str(MAX_ITEMS) in result.content


def test_a_context_with_no_plan_store_says_so_rather_than_raising():
    ctx = ToolContext(agent_id="o", cell=None, graph=None, files=None,
                      permissions=None, network_allowed=False, todos=None)
    result, _ = _write([("a", PENDING)], ctx)
    assert result.is_error and "no plan store" in result.content


# ── Survival through compaction — the reason any of this exists ─────────────


def test_the_plan_is_restated_into_the_summary():
    from forge.warden.engine import Warden

    todos = TodoList()
    todos.replace([Todo("read the failing test", COMPLETED),
                   Todo("fix the parser", IN_PROGRESS),
                   Todo("run the suite", PENDING)])

    carried = Warden._carry_plan.__get__(_Stub(_ctx(todos)))("...earlier work...")

    assert "...earlier work..." in carried, "the summary itself must survive"
    assert "fix the parser" in carried
    assert "run the suite" in carried
    assert "Continue from the first unfinished item" in carried


def test_an_empty_plan_leaves_the_summary_untouched():
    from forge.warden.engine import Warden

    carried = Warden._carry_plan.__get__(_Stub(_ctx()))("just the summary")
    assert carried == "just the summary"


def test_a_context_without_a_plan_store_does_not_break_compaction():
    from forge.warden.engine import Warden

    ctx = ToolContext(agent_id="o", cell=None, graph=None, files=None,
                      permissions=None, network_allowed=False, todos=None)
    assert Warden._carry_plan.__get__(_Stub(ctx))("summary") == "summary"


def test_the_carried_plan_survives_rebuild_as_one_user_message():
    """rebuild merges task+summary; a second message would break alternation."""
    from forge.warden.compaction import rebuild

    todos = TodoList()
    todos.replace([Todo("finish the port", PENDING)])
    messages = [
        {"role": "user", "content": "do the port"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "more"},
    ]
    from forge.warden.engine import Warden

    summary = Warden._carry_plan.__get__(_Stub(_ctx(todos)))("did some of it")
    out = rebuild(messages, 3, summary)

    assert out[0]["role"] == "user"
    assert "finish the port" in out[0]["content"]
    # Strict alternation preserved.
    roles = [m["role"] for m in out]
    assert all(a != b for a, b in zip(roles, roles[1:]))


class _Stub:
    """Just enough Warden for _carry_plan, which only reads self.ctx."""

    def __init__(self, ctx):
        self.ctx = ctx


# ── The list itself ─────────────────────────────────────────────────────────


def test_counts_and_unfinished():
    todos = TodoList()
    todos.replace([Todo("a", COMPLETED), Todo("b", IN_PROGRESS), Todo("c", PENDING)])

    assert todos.counts() == (1, 1, 1)
    assert [t.content for t in todos.unfinished()] == ["b", "c"]


def test_an_empty_list_is_falsey_so_compaction_can_skip_it():
    assert not TodoList()
    todos = TodoList()
    todos.replace([Todo("a", PENDING)])
    assert todos
