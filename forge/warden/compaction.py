"""Reclaiming context (H5/H6).

Two layers, cheapest first, because losing granularity is a real cost and should
be paid only when the cheap layer cannot close the gap.

**Elision.** Old tool *results* are replaced with a stub, keyed by
`tool_use_id` and without ever inspecting their content. No model call, no
summarization, nothing lost that the workspace does not still hold — the files
are on disk and can be re-read. On a coding job most of the window is tool output,
so this alone often clears the threshold while leaving the reasoning verbatim.

**Summarization.** Only what elision could not reach. The transcript splits into
head (the original task, never touched), body (summarized away), and tail (the
most recent complete tool cycles, kept verbatim).

**The cut is legal by construction.** It falls only on an assistant message that
opens a tool cycle, which means the message before it is that cycle's
predecessor's `tool_result` — so a `tool_use` is never separated from its
result. That separation is the transcript-corruption failure mode, and it is
prevented by where the cut is allowed to land rather than by validating
afterwards.

**Failure is bounded.** Three consecutive failures and Forge stops trying. The
reference logs sessions that retried compaction thousands of times against a
context that could not be reduced — an unbounded compaction retry is a money
fire, not a resilience feature.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from forge.model.base import Model, TextDelta
from forge.warden import images

logger = logging.getLogger("forge.warden")

ELIDED = "[earlier tool output elided to reclaim context — the workspace still " \
         "has the files; re-read or re-run if you need this]"

# Tool cycles kept verbatim at the tail. Enough to see what was just tried and
# what it returned, which is what the next turn actually reasons from.
KEEP_CYCLES = 5
# Cycles whose results survive elision. Smaller than KEEP_CYCLES because elision
# is the cheap layer and should bite sooner.
ELIDE_AFTER_CYCLES = 3

MAX_COMPACT_FAILURES = 3

# What a forced recovery tries, in order, when the provider has already refused
# the request. Proactive compaction can afford to preserve a comfortable tail;
# a recovery cannot, because "nothing was reducible at my preferred settings" is
# not an outcome — it ends the job. Escalates to keeping no tail at all, which
# is severe and still better than dying with the work unfinished.
FORCED_ELIDE_KEEP = 1
FORCED_CUT_KEEPS = (2, 1, 0)

SUMMARY_INSTRUCTION = """\
Summarize the execution transcript above for an agent that will continue this
work with no other memory of it. Be thorough about technical specifics: this
summary replaces the transcript entirely.

Cover, in order:

1. Primary request and intent — every explicit instruction, in detail.
2. Key technical concepts, technologies and conventions in play.
3. Files created, modified or examined — path, why it mattered, what changed.
   Include the code that matters verbatim.
4. Errors hit and how each was resolved. This is the section that stops the
   next agent repeating a mistake, so do not compress it.
5. Problems solved, and any troubleshooting still in flight.
6. Every instruction and correction from the operator, verbatim. Their changing
   intent is the one thing that cannot be reconstructed from the workspace.
7. Pending work that was explicitly asked for and is not done.
8. What was being worked on immediately before this summary, precisely.
9. The next step, if there is an obvious one — quoting the request it serves, so
   the intent cannot drift.

Omit: file contents already written to disk, dead-end exploration, tool chatter.
Write the summary and nothing else."""


# ── Shape predicates ─────────────────────────────────────────────────────────
def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def is_tool_result_message(message: dict[str, Any]) -> bool:
    """A user message that closes a tool cycle."""
    return message.get("role") == "user" and any(
        b.get("type") == "tool_result" for b in _blocks(message))


def opens_a_cycle(message: dict[str, Any]) -> bool:
    """An assistant message that requests tools."""
    return message.get("role") == "assistant" and any(
        b.get("type") == "tool_use" for b in _blocks(message))


# ── Layer one: elision ───────────────────────────────────────────────────────
def elide_old_tool_results(
    messages: list[dict[str, Any]], keep_cycles: int = ELIDE_AFTER_CYCLES
) -> tuple[list[dict[str, Any]], int]:
    """Replace the content of tool results older than the last `keep_cycles`.

    Keyed entirely on position and `tool_use_id`; the content is never examined,
    only substituted. That is what keeps this composable with everything else —
    it cannot disagree with another layer about what a result *meant*.

    Returns the new transcript and how many characters were reclaimed."""
    carriers = [i for i, m in enumerate(messages) if is_tool_result_message(m)]
    if len(carriers) <= keep_cycles:
        return messages, 0

    doomed = set(carriers[:-keep_cycles] if keep_cycles else carriers)
    freed = 0
    out: list[dict[str, Any]] = []
    for i, message in enumerate(messages):
        if i not in doomed:
            out.append(message)
            continue
        rebuilt = []
        for block in _blocks(message):
            if block.get("type") == "tool_result" and block.get("content") != ELIDED:
                freed += len(str(block.get("content", "")))
                rebuilt.append({**block, "content": ELIDED})
            else:
                rebuilt.append(block)
        out.append({**message, "content": rebuilt})
    return out, freed


# ── What summarization is not allowed to paraphrase ──────────────────────────
# The summary prompt asks for the operator's instructions verbatim, and a
# capable model obliges. A rule the model can decline is not a mechanism,
# though, and this is the one place where declining it is invisible: the
# session does not error, it quietly starts working on a slightly different
# problem than the one that was asked for. On a six-hour run that is the
# difference between finishing and drifting.
#
# So the operator's turns are carried by the harness, which cannot paraphrase
# because it is not reading them. The prompt still asks — belt and braces, and
# the model's rendering has context the copy does not.
OPERATOR_TURNS_CAP = 4_000


def operator_turns(messages: list[dict[str, Any]]) -> list[str]:
    """The operator's own words, in order, from a transcript slice.

    A user-role message is the operator's only if it is not carrying tool
    results. That single test is exact here rather than heuristic: the loop's
    own user-role injections — the verification nudge, the wind-down request,
    the interrupt marker — are all appended after a run begins, while the
    operator's turns arrive only in the transcript a run is seeded with. Which
    means the caller decides what to pass, and the caller knows.
    """
    out: list[str] = []
    for message in messages:
        if message.get("role") != "user" or is_tool_result_message(message):
            continue
        content = message.get("content")
        text = (content if isinstance(content, str)
                else "\n".join(b.get("text", "") for b in _blocks(message)
                               if b.get("type") == "text"))
        if text and text.strip():
            out.append(text.strip())
    return out


def render_operator_turns(turns: list[str], cap: int = OPERATOR_TURNS_CAP) -> str:
    """Turns as a verbatim block, newest-first under a size ceiling.

    Dropped from the OLDEST end when it will not fit, which is the opposite of
    how the rest of compaction treats age. Deliberate: an operator's later
    instruction usually supersedes an earlier one, so if intent has to be lost
    it should be the intent that was already overtaken. The drop is stated
    rather than silent — a summary that has quietly shed half the brief while
    reading as complete is worse than one that admits the gap."""
    if not turns:
        return ""
    kept: list[str] = []
    budget = cap
    for turn in reversed(turns):
        if len(turn) > budget:
            break
        kept.append(turn)
        budget -= len(turn)
    kept.reverse()
    dropped = len(turns) - len(kept)
    if not kept:
        return ""
    body = "\n\n".join(f"> {t}" for t in kept)
    note = (f"[{dropped} earlier operator message(s) omitted for length]\n\n"
            if dropped else "")
    return ("EVERY INSTRUCTION THE OPERATOR GAVE, VERBATIM — carried through "
            "compaction unaltered, because a paraphrase of intent is a change "
            "to it:\n\n" + note + body)


# ── Layer two: summarization ─────────────────────────────────────────────────
def find_cut(messages: list[dict[str, Any]], keep_cycles: int = KEEP_CYCLES) -> int | None:
    """Index where the preserved tail begins, or None if there is nothing to gain.

    Only an assistant message that opens a tool cycle is eligible. Everything
    before such a message is a completed cycle, so cutting there can never
    orphan a `tool_use` from its `tool_result`."""
    starts = [i for i, m in enumerate(messages) if opens_a_cycle(m)]
    if len(starts) <= keep_cycles:
        return None
    cut = starts[-keep_cycles] if keep_cycles else len(messages)
    # Head is messages[0]; a cut at index 1 would summarize nothing.
    return cut if cut > 1 else None


def render_for_summary(messages: list[dict[str, Any]]) -> str:
    """Flatten a transcript slice to text for the summarization call.

    Rendering rather than replaying the structured messages is deliberate. A
    replay has to reproduce tool_use/tool_result pairing exactly or the API
    rejects it, and the summarizer gains nothing from that structure. Text
    cannot be malformed. The cost is that the call cannot share a prompt cache
    with the main conversation — worth paying while correctness is cheaper to
    verify than to debug."""
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content")
        if isinstance(content, str):
            lines.append(f"{role.upper()}: {content}")
            continue
        for block in _blocks(message):
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                lines.append(f"{role.upper()}: {block['text']}")
            elif kind == "image":
                # The summarizer call is text-only, so the picture cannot go —
                # but its ABSENCE must be visible. Dropped silently, a summary
                # reads as though the operator asked about nothing, and the
                # rebuilt transcript loses the fact that there was ever an
                # image to refer back to.
                lines.append(f"{role.upper()}: {images.describe(block)}")
            elif kind == "tool_use":
                try:
                    args = json.dumps(block.get("input", {}), default=str)[:2_000]
                except (TypeError, ValueError):
                    args = str(block.get("input"))[:2_000]
                lines.append(f"ASSISTANT calls {block.get('name')}({args})")
            elif kind == "tool_result":
                body = str(block.get("content", ""))
                flag = " [ERROR]" if block.get("is_error") else ""
                lines.append(f"TOOL RESULT{flag}: {body}")
    return "\n".join(lines)


async def summarize(model: Model, transcript: str, signal: asyncio.Event) -> str | None:
    """One tool-free model call. Returns None if it produced nothing.

    No tools are offered, so the model cannot spend its only turn on a tool call
    it would have to be refused — the reference needs a hard textual preamble
    against exactly that, because its summarizer inherits the parent's toolset
    for cache reasons. Forge does not, so the guarantee is structural."""
    reply: list[str] = []
    async for event in model.stream(
        system="You summarize engineering transcripts precisely and completely.",
        messages=[{"role": "user", "content": f"{transcript}\n\n---\n\n{SUMMARY_INSTRUCTION}"}],
        tools=[],
        signal=signal,
    ):
        if isinstance(event, TextDelta):
            reply.append(event.text)
    text = "".join(reply).strip()
    return text or None


def rebuild(
    messages: list[dict[str, Any]], cut: int, summary: str,
    said: list[str] | None = None,
) -> list[dict[str, Any]]:
    """(task + summary + what the operator said) as one message, then the tail.

    The task and the summary are merged rather than kept as two messages: the
    tail begins with an assistant turn, so a separate summary message would put
    two user messages back to back. Merging keeps the transcript strictly
    alternating and keeps the original instruction where it has always been —
    first, and never summarized.

    `said` is every operator turn, copied rather than summarized. Turns already
    inside the preserved head are dropped: the first one is the task itself, and
    on a second compaction the head already contains the block this produced —
    without the check, each pass would stack another copy of the same words."""
    replaced = len(messages[1:cut])
    head = messages[0].get("content")
    task = head if isinstance(head, str) else render_for_summary([messages[0]])
    verbatim = render_operator_turns([t for t in (said or []) if t not in task])
    return [
        {
            "role": "user",
            "content": (
                f"{task}\n\n"
                f"[COMPACTED — {replaced} earlier messages replaced by this summary]\n\n"
                f"{summary}\n\n"
                + (f"{verbatim}\n\n" if verbatim else "")
                + "[The transcript resumes below. Every file on disk reflects ALL of "
                "the work above, including the summarized part. Re-read any file "
                "before editing it — your memory of its contents is this summary's, "
                "not the file's.]"
            ),
        },
        *messages[cut:],
    ]
