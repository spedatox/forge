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
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from forge.warden.tool import Tool, ToolContext

logger = logging.getLogger(__name__)

# How many subagents may run at once. The parent can emit several `task` calls
# in one turn and the loop executes parallel-safe tools concurrently, so without
# a cap one turn could open an unbounded number of model streams.
MAX_CONCURRENT = 4

# A subagent is a means, not an end: it reports back and stops. Lower than the
# parent's ceiling on purpose — but not so low that a genuine search across a
# large repo gets cut off and reported as partial, which is the failure that
# makes delegation not worth the round trip.
SUBAGENT_MAX_ITERATIONS = 60


_VERDICT = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL|PARTIAL)\s*$",
                      re.MULTILINE | re.IGNORECASE)
_RAN_A_COMMAND = re.compile(r"^\s*RAN\b", re.MULTILINE | re.IGNORECASE)


def audit_verification(report: str) -> str:
    """Refuse a PASS that shows no evidence.

    The prompt asks for a command and its output behind every check. Asking is
    not enough — "verification avoidance" is precisely the habit of narrating a
    check instead of running it, and a report that says PASS with reasoning
    underneath reads exactly like one that ran something.

    So the claim is checked mechanically: a PASS with no `RAN` line never
    reaches the parent as a PASS. This cannot tell whether the commands were
    the RIGHT ones — only that some were run — but it closes the failure that
    costs the most, which is a confident all-clear nobody executed.
    """
    match = _VERDICT.search(report or "")
    if match is None:
        return (report + "\n\n[harness] This report has no VERDICT line, so it "
                "is not a verification result. Treat the work as unverified.")
    if match.group(1).upper() != "PASS":
        return report
    if _RAN_A_COMMAND.search(report or ""):
        return report
    return (report + "\n\n[harness] REJECTED: this PASS cites no command it "
            "ran. Reading code is not verification, and a check with no "
            "command is a skip reported as a pass. Treat the work as "
            "UNVERIFIED and check it yourself, or delegate again asking "
            "explicitly for the commands and their output.")


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

_VERIFY_PROMPT = """\
You verify that work actually functions. Your job is not to confirm it works — \
it is to try to break it.

Two things go wrong when an agent verifies its own work, and both were observed \
in this repository:

1. **Rationalising an anomaly.** A tool reported 22 import edges across 76 \
modules. The true number was 175. Instead of asking whether 22 was plausible, \
the implementer wrote a paragraph explaining why it made sense — and shipped \
"0 cycles found" as a clean result. If you catch yourself writing an \
explanation for a surprising number, stop and measure it against something \
independent instead.

2. **Trusting the implementer's tests.** They were written by an agent, against \
fixtures that agent chose, and they may only exercise the cases it already \
handled. A green suite is context, not evidence. Run it, note it, then go and \
verify the thing itself.

You cannot edit anything. That is deliberate: your value is the report, and an \
agent that fixes as it goes stops looking.

## How to verify

- Exercise the change directly — run the program, call the endpoint, invoke the \
CLI with real input. Reading the code is not verification.
- Check outputs against what they SHOULD be, not merely that they exist. \
"Returned 200" and "did not crash" are not results.
- Sanity-check every count and every total. Ask what the number ought to be \
roughly, get that figure independently, and compare. This one habit would have \
caught the failure above in a single command.
- Then try to break it: empty input, zero, one, a very large value, unicode, a \
malformed file, the same operation twice, an ID that does not exist.

## What your report must contain

For each check, all three of these:

    CHECK   what you are verifying
    RAN     the exact command
    SAW     the actual output, pasted, not paraphrased
    RESULT  PASS, or FAIL with expected vs actual

A check with no command is not a PASS — it is a skip, and reporting it as a \
PASS is worse than omitting it. At least one of your checks must be an attempt \
to break the thing, and you must report what happened even if it held.

End with exactly one line, on its own:

    VERDICT: PASS       everything you checked worked
    VERDICT: FAIL       something is broken — say what, with the output
    VERDICT: PARTIAL    you could not check something for an ENVIRONMENTAL \
reason (no test runner, tool missing, server would not start)

PARTIAL is not for "I am unsure". If you were able to run the check, decide. \
Before saying FAIL, check the behaviour is not deliberate — comments, \
AGENTS.md, or defensive code elsewhere may make it correct.
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
    "verify": SubagentSpec(
        name="verify",
        description=(
            "Independently checks that work you just finished actually "
            "functions, by running it rather than reading it, and returns a "
            "PASS/FAIL/PARTIAL verdict with the commands and output that "
            "back it. Use it before you report a non-trivial change as done — "
            "it starts from a fresh context, so unlike you it has not spent "
            "the last hour becoming convinced the code is right, and it is "
            "told to sanity-check counts and totals rather than accept them. "
            "It cannot edit anything, so it will not quietly fix what it "
            "finds. Do NOT use it in place of running your own tests, and do "
            "not use it on a one-line change where the cost exceeds the doubt."
        ),
        system_prompt=_VERIFY_PROMPT,
        tool_names=_EXPLORE_TOOLS + ("run_command", "diagnostics"),
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

    def _scoped_emit(self, spec: SubagentSpec, run_id: str, label: str):
        """The child's events, re-channelled so they cannot speak for the parent.

        A child Warden is a full loop and emits everything a parent does —
        including `done` when it finishes. Handing it the parent's emit meant
        that frame went straight out to the client, which reads `done` as the
        end of the turn: Heartbreaker closed the stream the moment the subagent
        finished, while the parent carried on working invisibly and still
        billing the provider.

        Nothing from a child now appears on the parent's channel. It all moves
        to `subagent`, a channel of its own that carries the run's id so a
        client can group several concurrent delegations, and which no consumer
        treats as terminal. That is what lets the work be SHOWN — in its own
        panel, foldable, ignorable — without it ever being mistaken for the
        answer.

        Dropping these frames entirely (the first fix) stopped the bug and cost
        the operator all visibility of a delegation that can run for minutes.
        """
        async def emit(event: dict) -> None:
            if self.emit is None:
                return
            etype = str(event.get("type", ""))
            payload: dict = {"id": run_id, "agent": spec.name, "label": label}

            if etype == "chunk":
                payload |= {"phase": "text", "text": event.get("data") or ""}
            elif etype == "tool":
                data = dict(event.get("data") or {})
                payload |= {"phase": "tool", "tool": data.get("name"),
                            "input": data.get("input")}
            elif etype == "tool_result":
                data = dict(event.get("data") or {})
                payload |= {"phase": "tool_result", "tool_use_id": data.get("id"),
                            "result": data.get("result")}
            elif etype in ("done", "error"):
                # Deliberately NOT forwarded as done/error: a child ending is
                # not the turn ending. `finished` is reported by run() once the
                # Terminal has been classified, which knows more than this does.
                return
            else:
                return                  # usage and the like stay harness-side

            await self.emit({"type": "subagent", "data": payload})

        return emit

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

    async def _announce(self, payload: dict) -> None:
        if self.emit is not None:
            await self.emit({"type": "subagent", "data": payload})

    async def run(self, spec: SubagentSpec, prompt: str,
                  label: str = "") -> tuple[str, bool]:
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

        run_id = uuid.uuid4().hex[:12]
        base = {"id": run_id, "agent": spec.name, "label": label}

        async def _finish(report: str, failed: bool) -> tuple[str, bool]:
            """Close the panel with the same text the parent receives, so what
            the operator reads and what the model reads cannot disagree."""
            await self._announce(base | {"phase": "finished", "ok": not failed,
                                         "report": report})
            return report, failed

        async with self._slots:
            await self._announce(base | {"phase": "started", "prompt": prompt})
            warden = self.build_warden(
                system_prompt=spec.system_prompt,
                tools=tools,
                max_iterations=SUBAGENT_MAX_ITERATIONS,
                emit=self._scoped_emit(spec, run_id, label),
            )
            try:
                terminal = await warden.run(prompt)
            except Exception as e:  # noqa: BLE001 — a child must not kill the parent
                logger.exception("subagent_failed")
                return await _finish(
                    f"The {spec.name} subagent failed: {type(e).__name__}: {e}", True)

        text = (terminal.final_text or "").strip()
        reason = terminal.reason

        if reason == StopReason.ERROR:
            return await _finish(
                f"The {spec.name} subagent errored: {terminal.error or 'no detail'}", True)
        if reason == StopReason.ABORTED:
            # The operator interrupted. Report the partial rather than dressing
            # an abort up as a finding.
            return await _finish(
                f"The {spec.name} subagent was interrupted before it finished. "
                f"Partial findings:\n\n{text or '(nothing yet)'}", True)
        if reason == StopReason.MAX_ITERATIONS:
            # Partial work is still work — hand back whatever it reached, and
            # say it is partial so the parent does not treat it as complete.
            return await _finish(
                f"The {spec.name} subagent hit its {SUBAGENT_MAX_ITERATIONS}"
                "-iteration ceiling before finishing. Partial findings:\n\n"
                f"{text or '(it produced no report)'}", False)
        if not text:
            return await _finish(
                f"The {spec.name} subagent finished without reporting anything. "
                "Treat this as no result, not as success.", True)
        if spec.name == "verify":
            # A verifier's whole product is a claim about reality, so the claim
            # is checked before it reaches the parent (see audit_verification).
            text = audit_verification(text)
        return await _finish(text, False)
