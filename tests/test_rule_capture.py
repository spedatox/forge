"""Stated a standing rule, saved nothing.

The complaint that started this: the owner tells an agent how he wants his work
done — "keep the workspace clean and structured" — the agent obeys it for the
turn, and by the next session it is gone, because nothing was written down. The
memory-protocol fragment tells the agent to file such a rule; this is the
backstop for the turn it does not.

So before a job ends, if the owner's message THIS turn reads as a standing rule
and nothing was persisted to memory, the loop asks once — the same shape as the
verification gate next to it: nudge once, continue the turn so the write can
happen now, and let the agent decline in words if it was not a rule after all.
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden import reminders
from forge.warden.engine import _RULE_CAPTURE_PROMPT, Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult


class _Args(BaseModel):
    model_config = ConfigDict(extra="allow")
    command: str | None = None
    content: str | None = None


class _Memory(Tool):
    name = "memory"
    description = "x" * 40
    Args = _Args

    async def call(self, args, ctx) -> ToolResult:
        return ToolResult(content="written")


class _Remember(Tool):
    name = "remember_about_owner"
    description = "x" * 40
    Args = _Args

    async def call(self, args, ctx) -> ToolResult:
        return ToolResult(content="noted")


def _ctx() -> ToolContext:
    return ToolContext(agent_id="optimus", cell=None, graph=None,
                       files=FileStateCache(), permissions=PermissionEngine(),
                       network_allowed=False)


def _warden(steps, *, tools=None) -> Warden:
    tools = {"memory": _Memory(), "remember_about_owner": _Remember()} if tools is None else tools
    return Warden(system_prompt="", tools=tools, model=ScriptedModel(steps),
                  ctx=_ctx(), max_iterations=12)


def _asked(term) -> bool:
    return any(_RULE_CAPTURE_PROMPT in str(m.get("content", "")) for m in term.messages)


def _asked_count(term) -> int:
    return sum(1 for m in term.messages if _RULE_CAPTURE_PROMPT in str(m.get("content", "")))


# ── The gate fires when a rule is stated and nothing is written ──────────────

def test_a_stated_rule_that_is_not_saved_is_questioned():
    """The whole point: a rule obeyed once and never recorded is the failure."""
    steps = [
        lambda m: ("Done — I'll keep it clean.", []),
        lambda m: ("Recorded it to dossier/wants.md.",
                   [tool_call("memory", command="str_replace", path="/memories/dossier/wants.md",
                              old_str="a", new_str="b")]),
        lambda m: ("Filed.", []),
    ]
    term = asyncio.run(_warden(steps).run("keep the workspace clean and structured"))

    assert _asked(term), "the loop let a stated rule end unsaved"
    assert term.reason is StopReason.COMPLETED


def test_writing_the_rule_to_memory_is_left_alone():
    """The agent did the right thing in-turn; nudging would be nagging."""
    steps = [
        lambda m: ("Filing that now.",
                   [tool_call("memory", command="create", path="/memories/dossier/wants.md",
                              file_text="- keep it clean")]),
        lambda m: ("Recorded.", []),
    ]
    term = asyncio.run(_warden(steps).run("always keep the workspace tidy"))

    assert not _asked(term)
    assert term.reason is StopReason.COMPLETED


def test_remember_about_owner_also_counts_as_saving():
    """The offline capture path is a save too — it reaches memory on reconnect."""
    steps = [
        lambda m: ("Queuing it.", [tool_call("remember_about_owner",
                                             content="Wants the workspace kept clean.")]),
        lambda m: ("Noted.", []),
    ]
    term = asyncio.run(_warden(steps).run("from now on keep things structured"))

    assert not _asked(term)


def test_reading_memory_is_not_saving_it():
    """A `memory view` looked something up; it did not write the rule down. A
    naive 'did it touch the memory tool' would be satisfied here and let an
    unsaved rule through."""
    steps = [
        lambda m: ("Let me check what's there.",
                   [tool_call("memory", command="view", path="/memories/dossier/wants.md")]),
        lambda m: ("It's already covered.", []),
        lambda m: ("Filed the addition.",
                   [tool_call("memory", command="str_replace", path="/memories/dossier/wants.md",
                              old_str="a", new_str="b")]),
        lambda m: ("Done.", []),
    ]
    term = asyncio.run(_warden(steps).run("never leave build artifacts lying around"))

    assert _asked(term)


def test_an_ordinary_task_is_not_questioned():
    """A request to do a piece of work is not a standing rule. Demanding a memory
    write for it would train the agent to ignore the ask."""
    steps = [lambda m: ("Fixed the parser bug.", [])]
    term = asyncio.run(_warden(steps).run("fix the crash in parser.py"))

    assert not _asked(term)
    assert term.reason is StopReason.COMPLETED


def test_the_question_is_asked_once_and_not_again():
    """'That was not a standing rule' is sometimes the right answer to the first
    ask, and a second ask is nagging."""
    steps = [
        lambda m: ("Understood, will do.", []),
        lambda m: ("Actually that was a one-off for this task, not a standing rule.", []),
    ]
    term = asyncio.run(_warden(steps).run("make sure to run the linter this time"))

    assert _asked_count(term) == 1
    assert term.reason is StopReason.COMPLETED


class _Other(Tool):
    name = "run_command"
    description = "x" * 40
    Args = _Args

    async def call(self, args, ctx) -> ToolResult:
        return ToolResult(content="ok")


def test_no_capture_tool_means_no_nudge():
    """With nowhere to send the agent, the nudge is the harness talking to
    itself. A profile without memory or remember_about_owner is never asked."""
    steps = [lambda m: ("Will keep it clean.", [])]
    term = asyncio.run(_warden(steps, tools={"run_command": _Other()})
                       .run("always keep it tidy"))

    assert not _asked(term)


def test_the_final_answer_is_the_one_after_saving():
    """The reply the owner reads should be the turn that recorded the rule, not
    the optimistic one that triggered the ask."""
    steps = [
        lambda m: ("Done.", []),
        lambda m: ("Recorded to dossier/wants.md.",
                   [tool_call("memory", command="str_replace", path="/memories/dossier/wants.md",
                              old_str="a", new_str="b")]),
        lambda m: ("Filed it — I'll keep the workspace clean going forward.", []),
    ]
    term = asyncio.run(_warden(steps).run("keep the workspace clean and structured"))

    assert "Filed it" in term.final_text


# ── The ask names the honest way out ─────────────────────────────────────────

def test_the_prompt_offers_the_decline_and_forbids_prose_only():
    """Otherwise it reads as 'always write something' and the agent files noise
    to satisfy it — worse than the silence it replaced."""
    assert "NOT a standing rule" in _RULE_CAPTURE_PROMPT
    assert "'noted' is not a memory write" in _RULE_CAPTURE_PROMPT
    # and it names both capture paths
    assert "memory" in _RULE_CAPTURE_PROMPT
    assert "remember_about_owner" in _RULE_CAPTURE_PROMPT


# ── The detector, directly ───────────────────────────────────────────────────

def test_the_detector_catches_standing_phrasing_in_both_languages():
    yes = [
        "keep the workspace clean and structured",
        "always run the tests before you report",
        "never push directly to main",
        "from now on use tabs not spaces",
        "make sure to update the changelog",
        "her zaman testleri çalıştır",
        "bundan sonra branch'te çalış",
        "workspace'i temiz tut",
    ]
    for t in yes:
        assert reminders.looks_like_standing_rule(t), t


def test_the_detector_leaves_ordinary_requests_alone():
    no = [
        "fix the crash in parser.py",
        "what does this function do?",
        "can you keep the dev server running while I test?",
        "explain the retry logic",
        "",
    ]
    for t in no:
        assert not reminders.looks_like_standing_rule(t), t
