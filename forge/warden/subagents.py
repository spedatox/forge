"""Subagents — a second Warden, with its own context and a narrower reach.

The loop already had everything needed for this: `Warden` is "parameterized per
job with everything it needs injected; holds no agent identity of its own". A
subagent is therefore not a new engine, it is the same engine constructed with
a different system prompt, a subset of the tools, and — the part that matters —
its OWN message list.

That last point is the whole feature. Reading forty files to find one function
costs the parent forty files of context it will carry for the rest of the
session. Handing that to a subagent costs the parent one paragraph: the answer.
The isolation is the product; the parallelism is a bonus.

Two boundaries keep it from becoming a way to lose control of a run:

- **Depth one.** A subagent never receives the `task` tool, so it cannot spawn
  its own. Fan-out is bounded by what the parent asks for rather than by a
  recursion that looks reasonable at every individual step.
- **The allowlist is the reach.** `explore` has no write tools in its dict at
  all — not "is told not to write". A tool that is absent cannot be called by a
  model that decides the instructions do not apply to it.

Cost rolls up: the child shares the parent's TokenLedger, so a run's total is
what the run cost, not what its top-level turns cost.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from forge.warden.tool import Tool, ToolContext

logger = logging.getLogger(__name__)

# How many subagents may run at once. The parent can emit several `task` calls
# in one turn and the loop executes parallel-safe tools concurrently, so without
# a cap one turn could open an unbounded number of model streams.
MAX_CONCURRENT = 4

# A subagent is a means, not an end: it reports back and stops. This is lower
# than the parent's ceiling on purpose — a subagent still going at forty
# iterations has misunderstood its task, and the parent is better served by a
# partial answer it can react to than by a long silence.
SUBAGENT_MAX_ITERATIONS = 20


@dataclass(frozen=True)
class SubagentSpec:
    """One kind of subagent: who it is, and what it may touch."""

    name: str
    description: str
    """Shown to the model in the tool description. This is what it selects on,
    so it says when to use this type AND when not to."""
    system_prompt: str
    tool_names: tuple[str, ...]
    read_only: bool = False


_EXPLORE_PROMPT = """\
You are a search specialist working inside a codebase. You find things and \
report what you found.

This is a READ-ONLY task. You have no tools that modify anything — no write, \
no edit, no shell. That is deliberate, and it is not a restriction to work \
around: your caller has its own tools and will make the changes.

How to work:
- Start with graph_overview or graph_query to orient yourself before reading \
files. Reading twenty files to learn what one graph query would have told you \
is the slowest possible route.
- Then grep and glob to narrow, and read_file only once you know which file.
- Issue independent searches in the same turn — they run in parallel.

What to report:
- Concrete locations: file path and line number, not "somewhere in the auth \
module".
- What you actually saw, not what you infer is probably there. If you did not \
find something, say so plainly — "no caller of this function exists in the \
repo" is a useful, actionable answer, and a guess dressed as a finding is not.
- Be brief. Your caller is paying context for every word you return, which is \
the entire reason you were given a separate one.
"""

_REVIEW_PROMPT = """\
You are reviewing a change for defects. You read; you do not fix.

This is a READ-ONLY task and you have no tools that modify anything. Report \
what is wrong and let the caller decide what to do about it.

What counts as a finding:
- Something that will actually misbehave: a wrong result, a crash, a case the \
code does not handle, a resource left open.
- Be specific about HOW it fails. "This could be a problem" is not a finding. \
"With an empty list this raises IndexError at line 42" is.

What does not count:
- Style, naming and formatting preferences.
- Speculation about code you did not read.
- Restating what the code does as though it were a concern.

If the change looks correct, say so and stop. Reporting nothing is a valid and \
common outcome, and inventing a concern to look thorough wastes the caller's \
time and trust.
"""

_GENERAL_PROMPT = """\
You are completing one self-contained task on behalf of another agent, in its \
workspace, and reporting the result.

You have the full coding toolset. Use it to finish the task you were given — \
not to expand it. If you discover adjacent work that also needs doing, say so \
in your report rather than doing it: the caller has context you do not and may \
have excluded it on purpose.

Your reply is the entire product. The caller sees nothing of your work except \
the final message — not the files you read, not the commands you ran. So state \
what you did, what the outcome was, and anything the caller must know to carry \
on. If you could not finish, say exactly where you stopped and why. A clear \
account of a partial result is worth far more than a confident summary that \
does not match what is on disk.
"""

# Read-only sets exist as tool NAMES rather than a flag on the run, because the
# guarantee has to survive a model that decides the instructions are advisory.
_EXPLORE_TOOLS = ("read_file", "glob", "grep",
                  "graph_query", "graph_path", "graph_overview")

BUILT_INS: dict[str, SubagentSpec] = {
    "explore": SubagentSpec(
        name="explore",
        description=(
            "Read-only search specialist. Use it to locate code, trace how "
            "something is wired, or answer 'where is X' across many files — "
            "the searching costs its context instead of yours, and you get "
            "back only the answer. Do NOT use it to make changes (it has no "
            "write tools) or for a lookup you could do in one or two calls "
            "yourself, where spawning it is pure overhead."
        ),
        system_prompt=_EXPLORE_PROMPT,
        tool_names=_EXPLORE_TOOLS,
        read_only=True,
    ),
    "review": SubagentSpec(
        name="review",
        description=(
            "Read-only reviewer that hunts for defects in code you have "
            "written or are about to rely on, and reports them with the "
            "failure case spelled out. Use it after a non-trivial change, on "
            "a fresh context that has not talked itself into believing the "
            "code is correct. Do NOT use it to make the fixes — it cannot — "
            "and do not use it as a substitute for running the tests."
        ),
        system_prompt=_REVIEW_PROMPT,
        tool_names=_EXPLORE_TOOLS + ("run_command",),
    ),
    "general": SubagentSpec(
        name="general",
        description=(
            "A full-capability agent for a self-contained piece of work you "
            "want done in its own context — a multi-step change, an "
            "investigation that will read a lot, a chore you do not want in "
            "your transcript. It has the whole coding toolset. Do NOT use it "
            "for something needing your conversation's context, since it "
            "starts fresh and sees only the prompt you write, and do NOT use "
            "it for a single tool call you could make directly."
        ),
        system_prompt=_GENERAL_PROMPT,
        tool_names=(),        # () = everything the parent has, minus `task`
    ),
}


def spec_for(name: str) -> SubagentSpec | None:
    return BUILT_INS.get(name.strip().lower())


def catalogue() -> str:
    """The available types, for the tool description."""
    return "\n".join(f"- {s.name}: {s.description}" for s in BUILT_INS.values())


@dataclass
class SubagentRunner:
    """Builds and runs child Wardens.

    Constructed by whoever assembled the parent (the peer runner or the REPL),
    because only they hold the model and the tool set. Injected on ToolContext
    the same way the plan is, so the tool boundary stays "name, description,
    schema" and nothing about spawning leaks into it.
    """

    build_warden: Callable[..., object]
    """(system_prompt, tools, max_iterations) -> Warden. A callable rather than
    the pieces, so this module never has to know how a Warden is wired."""

    parent_tools: Callable[[], dict[str, "Tool"]]
    emit: Callable[[dict], Awaitable[None]] | None = None
    _slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT))

    def tools_for(self, spec: SubagentSpec) -> dict[str, "Tool"]:
        """The child's toolset: the allowlist, resolved against the parent's,
        and never including `task` itself (depth one)."""
        available = self.parent_tools()
        if spec.tool_names:
            chosen = {n: available[n] for n in spec.tool_names if n in available}
        else:
            chosen = dict(available)
        chosen.pop("task", None)
        return chosen

    async def run(self, spec: SubagentSpec, prompt: str) -> tuple[str, bool]:
        """Run one subagent to completion. Returns (report, is_error).

        Every failure comes back as a value rather than an exception: the
        parent is mid-turn and a subagent that could not finish is information
        it can act on, not a reason to end the run.
        """
        from forge.warden.state import StopReason

        tools = self.tools_for(spec)
        if not tools:
            return (f"The '{spec.name}' subagent has none of the tools it needs "
                    "in this deployment, so it was not run."), True

        async with self._slots:
            if self.emit is not None:
                await self.emit({"type": "chunk",
                                 "data": f"\n[{spec.name} subagent: running]\n"})
            warden = self.build_warden(
                system_prompt=spec.system_prompt,
                tools=tools,
                max_iterations=SUBAGENT_MAX_ITERATIONS,
            )
            try:
                terminal = await warden.run(prompt)
            except Exception as e:  # noqa: BLE001 — a child must not kill the parent
                logger.exception("subagent_failed")
                return f"The {spec.name} subagent failed: {type(e).__name__}: {e}", True

        text = (terminal.final_text or "").strip()
        reason = terminal.reason

        if reason == StopReason.ERROR:
            return f"The {spec.name} subagent errored: {terminal.error or 'no detail'}", True
        if reason == StopReason.ABORTED:
            # The operator interrupted. Report the partial rather than dressing
            # an abort up as a finding.
            return (f"The {spec.name} subagent was interrupted before it "
                    f"finished. Partial findings:\n\n{text or '(nothing yet)'}"), True
        if reason == StopReason.MAX_ITERATIONS:
            # Partial work is still work — hand back whatever it reached, and
            # say it is partial so the parent does not treat it as complete.
            return (f"The {spec.name} subagent hit its {SUBAGENT_MAX_ITERATIONS}"
                    "-iteration ceiling before finishing. Partial findings:\n\n"
                    f"{text or '(it produced no report)'}"), False
        if not text:
            return (f"The {spec.name} subagent finished without reporting "
                    "anything. Treat this as no result, not as success."), True
        return text, False
