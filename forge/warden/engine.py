"""The Warden loop engine (§3).

One `while True`. One mutable `LoopState`. One typed `Terminal`. The sole stop
condition is 'the model stopped requesting tools'. The interrupt signal is checked
at both boundaries — after the model responds and after tools execute — and yields
a clean, well-formed stop. Tools run concurrently only when every tool in the batch
declares itself concurrency-safe (§4 fail-closed).

The engine contains no identity strings (§2): system prompt, tool set, model, and
Cell all arrive as parameters. It is the same engine for every configured agent;
a new agent is added purely as configuration, never by editing this file.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from forge.model.base import Model, TextDelta, ToolUseRequest, TurnEnd, UsageReport
from forge.model.errors import ErrorClass, classify, retry_after
from forge.warden.compaction import (
    ELIDE_AFTER_CYCLES,
    FORCED_CUT_KEEPS,
    FORCED_ELIDE_KEEP,
    KEEP_CYCLES,
    MAX_COMPACT_FAILURES,
    elide_old_tool_results,
    find_cut,
    operator_turns,
    rebuild,
    render_for_summary,
    summarize,
)
from forge.warden.dispatch import dispatch_tool, to_anthropic_tool_result
from forge.warden.filestate import digest
from forge.warden import inbox as inbox_mod
from forge.warden.inbox import Inbox
from forge.warden.ledger import TokenLedger
from forge.warden.results import enforce_batch_budget
from forge.warden import reminders
from forge.warden.state import (
    ContinueReason, LoopState, StopReason, Terminal, Transition)
from forge.warden.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("forge.warden")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

# Ceiling on tools in flight at once. Bounds the Cell, not the model: the model may
# ask for any number of parallel reads: this decides how many actually run together.
MAX_TOOL_CONCURRENCY = 10

# Consecutive re-attempts at one failed turn, and the first backoff. Four
# attempts at 2s doubling covers ~30s of provider trouble, which is the shape of
# a 529 spike; past that it is an outage and failing loudly beats hanging on.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_S = 2.0

# Tools that change the tree, and tools that can prove a change works. The
# split is what lets the loop notice "wrote code, ran nothing" — the one
# failure a model cannot catch in itself, because from the inside a plan it
# never executed is indistinguishable from one it did.
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_CHECK_TOOLS = frozenset({"run_command"})

# What counts as persisting a durable fact to the owner's memory. `memory` with a
# write command reaches the store directly; `remember_about_owner` queues it for
# the store even with no channel. A `memory view` is a read and is not here — the
# point is whether a stated rule was WRITTEN DOWN, not merely looked up.
_MEMORY_WRITE_COMMANDS = frozenset({"create", "str_replace", "insert"})


def _persists_to_memory(tu) -> bool:
    """Whether one tool call wrote a durable fact to the owner's memory."""
    if tu.name == "remember_about_owner":
        return True
    if tu.name == "memory":
        return (tu.input or {}).get("command") in _MEMORY_WRITE_COMMANDS
    return False


_VERIFY_PROMPT = (
    "Before you finish: you changed files this turn and did not run anything "
    "afterwards, so nothing here has been shown to work.\n\n"
    "Run whatever actually exercises the change — the test suite, the specific "
    "test, the program, a type check, an import. Then report what you saw.\n\n"
    "If there is genuinely nothing to run, say so explicitly and say why, and "
    "tell the operator what you did NOT verify. An honest 'I could not check "
    "this' is useful; a confident summary of untested work is the single most "
    "expensive thing you can hand back, because it looks exactly like success."
)
_RULE_CAPTURE_PROMPT = (
    "Before you finish: the owner stated something this turn that reads as a "
    "STANDING rule — how he wants his work done from here on, not a one-off for "
    "this task — and you have not written it down. A rule you only obey this "
    "turn is gone by the next session; you are stateless between turns, and the "
    "owner's memory is the only thing that carries it forward.\n\n"
    "Record it NOW, in the same turn, in the one right place:\n"
    "  - a standing preference or working rule → append it to the matching "
    "`dossier/*` file with the `memory` tool (`view` it, then `str_replace` your "
    "line in), attributed and dated `[YYYY-MM-DD, <your agent id>]`;\n"
    "  - if you have no channel to Mark VI this run, use `remember_about_owner` "
    "instead — it queues the fact and reaches memory on reconnect.\n\n"
    "If what he said was genuinely NOT a standing rule — a one-off for this task, "
    "or something already recorded — say so in one line and finish. Do not just "
    "acknowledge the rule in prose: 'noted' is not a memory write, and next "
    "session there is no prose to read."
)
_MAX_BACKOFF_S = 30.0

# How many tracked files a staleness sweep will re-read. The cache holds 100;
# checking all of them after every command would turn a `pytest` into a hundred
# extra reads. The files at risk are the ones in play, and those are the ones
# the LRU has at the front.
_STALE_SWEEP_LIMIT = 20

# How many times one job may resume a turn cut off at the output cap. Past this
# the work is not fitting in the shape it is being written in, and a fourth
# continuation is money spent to arrive at the same place — the loop says so and
# stops rather than grinding.
MAX_TRUNCATION_RESUMES = 3

# Injected after a turn the provider cut off mid-flight. Told what NOT to do,
# because the three things a model does on being told it was truncated —
# apologise, recap, restart the paragraph — each cost another slice of the cap
# that just ran out, and the third can loop indefinitely.
_RESUME_TRUNCATED = (
    "Your previous turn was cut off at the output limit — what you wrote is "
    "incomplete, and the operator can see that it stops mid-flight.\n\n"
    "Resume directly. Do not apologise, do not recap what you were doing, and "
    "do not start the section again — pick up from exactly where the text "
    "stops, mid-sentence if that is where the cut fell. Break what remains "
    "into smaller pieces so the next turn finishes inside the limit: if you "
    "were writing a long file, write it in parts; if you were explaining, "
    "finish the current point and stop."
)


@dataclass
class _Turn:
    """One model turn, collected in full before any of it reaches the transcript.

    A turn that fails carries its exception here instead of raising, so the loop
    body decides what to do about it at a single, named boundary."""
    text: str = ""
    tool_uses: list[ToolUseRequest] = field(default_factory=list)
    usage: UsageReport | None = None
    error: Exception | None = None
    end: TurnEnd | None = None
    """Why the provider says the turn stopped. None when it did not say, which
    is NOT the same as a clean end — see `truncated`."""

    def truncated(self) -> bool:
        """Whether this turn was cut off at the output cap.

        False when the provider reported nothing. That is the fail-open
        direction and it is chosen deliberately: the alternative is treating
        every silent provider's every turn as truncated, which would resume
        forever. The cost of this choice is that providers reporting neither
        stop reason nor usage keep the old behaviour; the fix for them is to
        report one, not to guess here."""
        return self.end is not None and self.end.truncated()

    def assistant_message(self) -> dict[str, Any]:
        """Render the turn as one Anthropic assistant message. Empty turns still
        get a text block — the API rejects empty content."""
        content: list[dict[str, Any]] = []
        if self.text:
            content.append({"type": "text", "text": self.text})
        content.extend({"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input,
                        "reasoning_content": tu.reasoning_content}
                       for tu in self.tool_uses)
        return {"role": "assistant", "content": content or [{"type": "text", "text": ""}]}


async def _noop_emit(_event: dict[str, Any]) -> None:
    return None


class Warden:
    """The parameterized execution loop. Instantiated per job with everything it
    needs injected; holds no agent identity of its own."""

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: dict[str, Tool],
        model: Model,
        ctx: ToolContext,
        max_iterations: int = 200,
        signal: asyncio.Event | None = None,
        emit: Emit | None = None,
        max_tool_concurrency: int = MAX_TOOL_CONCURRENCY,
        ledger: TokenLedger | None = None,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_base_delay: float = RETRY_BASE_DELAY_S,
        refresh_tools: Callable[[], Awaitable[dict[str, Tool]]] | None = None,
        inbox: "Inbox | None" = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.ctx = ctx
        self.max_iterations = max_iterations
        self.signal = signal or asyncio.Event()
        self.emit = emit or _noop_emit
        self._tool_slots = asyncio.Semaphore(max_tool_concurrency)
        # Sized per job from the model's window; the default suits Anthropic.
        self.ledger = ledger or TokenLedger()
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self.refresh_tools = refresh_tools
        self.inbox = inbox
        """Where the operator's mid-run input waits. None in every headless
        path — a dispatched job has nobody at a keyboard, and an inbox that can
        never be filled is one more thing for the loop to check per iteration."""
        self._reminders = reminders.ReminderState()
        """Per-Warden, so a subagent's observations never leak into its
        parent's — a child that retried a failing call says nothing about
        whether the parent is stuck."""
        self._state: LoopState | None = None
        """The live loop's state, while it runs. Kept reachable so a caller that
        force-cancels the task can still recover everything the loop had already
        committed — a transcript that died mid-turn still costs only its own
        unfinished sentence, not every tool round that ran before the cancel."""

    async def run(self, task: str) -> Terminal:
        """Drive the loop for one job and return its single typed Terminal."""
        return await self.run_messages([{"role": "user", "content": task}])

    async def run_messages(self, messages: list[dict[str, Any]]) -> Terminal:
        """Same loop, seeded with an existing transcript.

        A dispatched job starts from one task; a conversation continues from
        everything already said. Both are the same loop over the same state —
        only where `state.messages` begins differs, which is why this is the
        real entry point and `run` is the one-message case of it."""
        state = LoopState(messages=list(messages), ledger=self.ledger,
                          operator_turns=operator_turns(messages))
        self._state = state
        tool_schemas = [t.schema() for t in self.tools.values()]

        while True:
            # ── Budget boundary: the single iteration ceiling (§3). ──────────
            # Retry laps are excluded: the ceiling bounds work attempted, and a
            # provider having a bad afternoon must not silently shorten the job.
            if state.iteration - state.retries >= self.max_iterations:
                return await self._wind_down(state)
            state.iteration += 1

            # ── Make room, before spending a turn discovering there is none. ──
            if state.ledger.should_compact():
                await self._compact(state)

            # ── Seam 1: a source that came up mid-job joins here. ────────────
            if await self._refresh_tools():
                tool_schemas = [t.schema() for t in self.tools.values()]

            # ── Act: stream one model turn, collecting text + tool-use blocks. ─
            turn = await self._stream_turn(state, tool_schemas)

            # ── Failure boundary ─────────────────────────────────────────────
            # Every way a turn can fail arrives here, and nowhere else. That is
            # the point of collecting the turn instead of inlining the stream:
            # recovery (retry a transient, compact and re-attempt) is a decision
            # made at one site over a discarded turn, not an except-block wrapped
            # around half the loop body.
            if turn.error is not None:
                if await self._recover(turn.error, state):
                    continue
                logger.error("model_stream_failed", exc_info=turn.error)
                await self.emit({"type": "error", "data": f"model error: {turn.error}"})
                return self._terminal(
                    StopReason.ERROR, state,
                    error=f"{type(turn.error).__name__}: {turn.error}")

            # The turn streamed cleanly; the bad patch, if there was one, is over.
            state.retry_attempt = 0

            # Account for the turn before the transcript grows. `state.messages`
            # is still exactly what was sent, which is what the estimate has to
            # measure when a provider reports nothing.
            if turn.usage is not None:
                state.ledger.record(turn.usage)
            else:
                state.ledger.estimate(self.system_prompt, state.messages)
            await self.emit({"type": "usage",
                             "data": {**state.ledger.snapshot(),
                                      "iteration": state.iteration}})

            # The turn is committed only now, once the stream has ended cleanly.
            # A turn that failed was never appended, so discarding it needs no
            # transcript surgery — and a retry cannot duplicate its text.
            if turn.text:
                state.last_text = turn.text
            state.messages.append(turn.assistant_message())

            # ── Boundary 1: interrupt checked after the model responds (§3). ──
            if self.signal.is_set():
                # Back-fill tool_results so the transcript stays well-formed even
                # though we did not run the tools (study §4).
                if turn.tool_uses:
                    state.messages.append(self._interrupted_results(turn.tool_uses))
                return self._aborted(state)

            # ── Stop condition: no tool-use blocks → the model is done (§3). ──
            # Unless it isn't. A turn cut off at the output cap has text and no
            # tool-use blocks, which is byte-for-byte what a finished turn looks
            # like from the transcript. Checked FIRST, before the verification
            # nudge: an unfinished turn has not earned the question of whether
            # its work was checked, and asking would bury the resume under a
            # second instruction.
            #
            # Only the no-tool-uses case is handled here. A turn truncated
            # mid-`tool_use` leaves that block's arguments incomplete, and that
            # already resolves itself one layer down — the partial input fails
            # `Args.model_validate` and comes back as a legible validation
            # error naming the signature. Intervening here would pre-empt a
            # better message.
            if not turn.tool_uses and turn.truncated():
                if state.truncation_resumes >= MAX_TRUNCATION_RESUMES:
                    logger.warning("truncation_resumes_exhausted",
                                   extra={"attempts": state.truncation_resumes})
                    return self._terminal(
                        StopReason.ERROR, state,
                        error=f"the model's output was cut off at the token limit "
                              f"{state.truncation_resumes + 1} times in a row; the last "
                              f"answer is incomplete. Raise FORGE_MAX_TOKENS, or ask for "
                              f"the work in smaller pieces.")
                state.truncation_resumes += 1
                # Charged as a retry: a resumed turn is not work done, and
                # letting truncation quietly extend the iteration ceiling is
                # the same leak `retries` exists to plug.
                state.retries += 1
                logger.info("turn_truncated",
                            extra={"attempt": state.truncation_resumes,
                                   "reason": turn.end.reason if turn.end else None})
                await self.emit({"type": "chunk",
                                 "data": f"\n[output limit reached — continuing "
                                         f"({state.truncation_resumes} of "
                                         f"{MAX_TRUNCATION_RESUMES})]\n"})
                state.messages.append({"role": "user", "content": _RESUME_TRUNCATED})
                state.advance(ContinueReason.RESUMED_TRUNCATED,
                              f"attempt {state.truncation_resumes}")
                continue

            # ── The operator said something while this was running. ──────────
            # Checked BEFORE the completion branch, because the most valuable
            # moment for an interjection is exactly the one where the model has
            # decided it is finished and the operator has not. Landing it here
            # continues the turn instead of ending it and making them start
            # another to say the same thing.
            if not turn.tool_uses and self.inbox:
                claimed = self.inbox.claim()
                if claimed:
                    state.messages.append(
                        {"role": "user", "content": inbox_mod.render(claimed)})
                    await self.emit({"type": "steered",
                                     "data": {"count": len(claimed), "at": "turn_end"}})
                    state.advance(ContinueReason.NEXT_TURN, "operator interjection")
                    continue

            if not turn.tool_uses:
                if self._unverified(state):
                    state.verification_nudged = True
                    state.messages.append({"role": "user", "content": _VERIFY_PROMPT})
                    state.transitions.append(
                        Transition(ContinueReason.NEXT_TURN, "unverified changes"))
                    continue
                # A standing rule the owner stated this turn and the agent is
                # about to walk away from without recording. Same shape as the
                # verification gate: nudge once, continue the turn so the write
                # can happen now, and let the agent decline in words if it was
                # not a rule after all.
                if self._unsaved_rule(state):
                    state.rule_capture_nudged = True
                    state.messages.append({"role": "user", "content": _RULE_CAPTURE_PROMPT})
                    state.transitions.append(
                        Transition(ContinueReason.NEXT_TURN, "unsaved standing rule"))
                    continue
                await self.emit({"type": "done", "data": turn.text})
                return self._terminal(StopReason.COMPLETED, state)

            # ── Observe: run the requested tools (parallel only where safe). ──
            self._note_tools(state, turn.tool_uses)
            results = await self._run_tools(turn.tool_uses)
            # Each result is already within its own cap; this is the batch as a
            # whole, which no single-result cap can see.
            results = await enforce_batch_budget(turn.tool_uses, results, self.tools, self.ctx)
            result_blocks = [to_anthropic_tool_result(tu.id, res)
                             for tu, res in zip(turn.tool_uses, results)]
            for tu, res in zip(turn.tool_uses, results):
                await self.emit({"type": "tool_result",
                                 "data": {"tool_use_id": tu.id, "is_error": res.is_error,
                                          "content": res.content,
                                          # Operator-facing only; see ToolResult.display.
                                          "display": res.display}})
            # A nudge rides with the results that earned it, so it arrives at
            # the moment it applies rather than competing with everything that
            # has happened since the system prompt was read.
            reminders.observe(self._reminders, state, turn.tool_uses, results)
            # A file moving underneath the model outranks any judgement about
            # how the run is going: it is a fact the model cannot observe, it
            # expires the moment it re-reads, and acting on stale contents is
            # the more expensive of the two mistakes. Leaving `due` uncalled
            # spends nothing — an unfired rule is still owed next turn.
            changed = await self._external_changes(turn.tool_uses)
            nudge = (reminders.file_changed_notice(changed) if changed
                     else reminders.due(self._reminders))
            if nudge:
                result_blocks = [*result_blocks, {"type": "text", "text": nudge}]
                logger.info("reminder_fired",
                            extra={"iteration": state.iteration, "files": changed})

            # Rides with the tool results rather than as its own message, for
            # the reason `inbox.py` gives at length: a separate user message
            # would break the strict alternation `find_cut` and `rebuild`
            # depend on. Appended last so it is the final thing read before the
            # model plans its next move.
            if self.inbox:
                claimed = self.inbox.claim()
                if claimed:
                    result_blocks = [*result_blocks,
                                     {"type": "text", "text": inbox_mod.render(claimed)}]
                    logger.info("operator_interjection",
                                extra={"iteration": state.iteration, "count": len(claimed)})
                    await self.emit({"type": "steered",
                                     "data": {"count": len(claimed), "at": "tool_results"}})

            state.messages.append({"role": "user", "content": result_blocks})

            # ── Boundary 2: interrupt checked after tools execute (§3). ───────
            if self.signal.is_set():
                return self._aborted(state)

            # Tools ran, so the loop is progressing rather than re-attempting.
            # Whatever truncation streak was in flight is over — the next cap
            # hit is a fresh one, not the fourth of a series.
            state.truncation_resumes = 0
            state.advance(ContinueReason.NEXT_TURN)

    # ── Noticing a file move underneath the model ────────────────────────────
    async def _external_changes(self, tool_uses: list[ToolUseRequest]) -> list[str]:
        """Files the model has read that no longer say what it read.

        The sweep is conditional, because the cost is one filesystem read per
        tracked file and the benefit is zero on a turn that could not have
        changed anything. A batch of nothing but read-only calls is exactly
        that turn, and on a long exploration it is most of them.

        `write_file` and `edit_file` are not excluded and do not need to be:
        both re-record their own new state, so a file they just wrote matches
        what the model was told and reports nothing. The turns that DO pay for
        this are the ones that ran a command, spawned a subagent, or entered a
        worktree — which is the same list as the ways a file actually changes.

        Bounded twice over: to the most recently used entries, and to whatever
        the Cell can still read. A file that vanished says nothing here —
        read-before-write will say it at the edit, where it is actionable.
        """
        cell = getattr(self.ctx, "cell", None)
        if cell is None or not self._could_have_touched_the_tree(tool_uses):
            return []

        changed: list[str] = []
        for path in self.ctx.files.tracked(limit=_STALE_SWEEP_LIMIT):
            try:
                content = await cell.read(path)
            except Exception:  # noqa: BLE001, S112 — see docstring: not this rule's business
                continue
            if self.ctx.files.note_external_change(path, digest(content)):
                changed.append(path)
        return changed

    def _could_have_touched_the_tree(self, tool_uses: list[ToolUseRequest]) -> bool:
        """Whether anything in this batch could have written to the workspace.

        Read from the class constant rather than the per-input override, and
        deliberately: `run_command` answers `is_read_only` per command, so
        `git status` would say no and skip the sweep. It would usually be
        right. The cost of being wrong in that direction is a stale edit; the
        cost of being wrong the other way is one extra stat-shaped read. Take
        the over-approximation."""
        return any(t is not None and not t.READ_ONLY
                   for t in (self.tools.get(tu.name) for tu in tool_uses))

    # ── Seam 1: the toolset can change between turns ─────────────────────────
    async def _refresh_tools(self) -> bool:
        """Re-ask the providers. True if the set of names changed.

        Only the *names* are compared, and the toolset is left alone when they
        match. Providers hand back fresh instances every call, so swapping on
        every turn would rebuild the schema array and change the bytes sent to
        the provider — busting the prompt cache on every iteration to achieve
        nothing. The names are what the model can see and what a new source
        actually adds."""
        if self.refresh_tools is None:
            return False
        try:
            latest = await self.refresh_tools()
        except Exception as e:  # noqa: BLE001 — a source that failed to reload
            logger.warning("tool_refresh_failed", extra={"error": repr(e)})
            return False        # keep running with the toolset we have
        if set(latest) == set(self.tools):
            return False
        added = sorted(set(latest) - set(self.tools))
        removed = sorted(set(self.tools) - set(latest))
        logger.info("toolset_changed", extra={"added": added, "removed": removed})
        self.tools = latest
        return True

    # ── Making room ──────────────────────────────────────────────────────────
    async def _compact(self, state: LoopState, forced: bool = False) -> bool:
        """Reclaim context. True if anything was freed.

        Cheap layer first: eliding old tool results costs no model call and
        keeps the reasoning verbatim. Only if that leaves the window still over
        the line does the transcript get summarized, because a summary is lossy
        and granular history is worth keeping when it fits.

        `forced` skips the after-elision threshold check: the caller is
        recovering from a provider that has already refused the request, so
        "probably enough" is not good enough."""
        if state.compact_failures >= MAX_COMPACT_FAILURES:
            logger.warning("compaction_disabled", extra={"failures": state.compact_failures})
            return False

        messages, freed = elide_old_tool_results(
            state.messages, FORCED_ELIDE_KEEP if forced else ELIDE_AFTER_CYCLES)
        if freed:
            state.messages = messages
            # The ledger's figure is from the last API call and cannot see this
            # yet. Adjust provisionally so the next turn's threshold check
            # reflects the reclaim; the next real UsageReport corrects it.
            state.ledger.prompt_tokens = max(0, state.ledger.prompt_tokens - freed // 4)
            await self.emit({"type": "compact",
                             "data": {"stage": "elide", "freed_chars": freed}})
            if not forced and not state.ledger.should_compact():
                self._forget_files()             # the cheap layer was enough
                return True

        cut = None
        for keep in (FORCED_CUT_KEEPS if forced else (KEEP_CYCLES,)):
            cut = find_cut(state.messages, keep)
            if cut is not None:
                break
        if cut is None:
            # Nothing summarizable. Not a failure — a short transcript that is
            # somehow too large has no structure to exploit, and saying so
            # beats burning a model call to discover it.
            logger.info("compaction_found_nothing_to_cut")
            if freed:
                self._forget_files()
            return bool(freed)

        await self.emit({"type": "compact", "data": {"stage": "summarize", "cut": cut}})
        try:
            summary = await summarize(
                self.model, render_for_summary(state.messages[1:cut]), self.signal)
        except Exception as e:  # noqa: BLE001 — a failed rescue must not be fatal
            logger.warning("compaction_call_failed", extra={"error": repr(e)})
            summary = None

        if summary is None:
            state.compact_failures += 1
            if freed:
                self._forget_files()
            return bool(freed)

        state.messages = rebuild(state.messages, cut, self._carry_plan(summary),
                                 state.operator_turns)
        state.compact_failures = 0
        self._forget_files()
        await self.emit({"type": "compact",
                         "data": {"stage": "done", "messages": len(state.messages)}})
        return True

    def _carry_plan(self, summary: str) -> str:
        """Re-state the plan into the summary, which compaction is keeping.

        The plan lives harness-side (warden/todos.py) but the model only reads
        the transcript, and the turns where it wrote the list are exactly what
        just got replaced. Without this the agent comes out of a compaction
        having forgotten which of its own steps are still outstanding — and the
        long jobs that need compaction are precisely the ones with a plan worth
        keeping. Appended to the summary rather than added as a message because
        `rebuild` merges task and summary into one, and a separate message would
        break the strict alternation it exists to preserve."""
        todos = getattr(self.ctx, "todos", None)
        if not todos:
            return summary
        return (
            f"{summary}\n\n{todos.render('PLAN CARRIED THROUGH COMPACTION')}\n"
            "The completed steps above are done — the work is on disk. Continue "
            "from the first unfinished item."
        )

    def _forget_files(self) -> None:
        """Drop read-before-write grounding after any reclamation.

        Not only after summarizing. Elision removes tool *results*, and a
        `read_file` result is one — so the model can lose a file's contents
        while the cache still reports "you have read this, you may edit it".
        That combination permits a blind edit against remembered text the model
        can no longer see. Any reclamation invalidates the grounding, so any
        reclamation clears it and the next edit has to look again."""
        self.ctx.files.clear()

    # ── Recovery: what to do about a turn that failed ────────────────────────
    async def _recover(self, error: Exception, state: LoopState) -> bool:
        """Decide whether the loop should try again. True means continue.

        The turn that failed was never committed to the transcript, so there is
        nothing to undo: a retry re-sends exactly the prompt the failed attempt
        was given, and cannot duplicate text the operator already saw streamed."""
        kind = classify(error)

        if kind is ErrorClass.RECOVERABLE:
            # The window is full. The same request will fail identically forever,
            # but a *smaller* request may not — so make one smaller and try
            # again. This is the backstop for the proactive threshold above,
            # which works off an estimate and can undershoot.
            if not await self._compact(state, forced=True):
                return False
            state.advance(ContinueReason.RECOVERED_CONTEXT, type(error).__name__)
            return True

        if kind is not ErrorClass.TRANSIENT:
            return False
        if state.retry_attempt >= self.retry_attempts:
            logger.warning("retries_exhausted",
                           extra={"attempts": state.retry_attempt, "error": repr(error)})
            return False

        state.retry_attempt += 1
        state.retries += 1
        delay = self._backoff_delay(state.retry_attempt, retry_after(error))
        logger.info("model_stream_retry",
                    extra={"attempt": state.retry_attempt, "delay_s": round(delay, 1),
                           "error": repr(error)})
        # The operator is watching a stream that just stopped mid-sentence. Say
        # why, or the restart of the visible text looks like the model glitching.
        await self.emit({"type": "chunk",
                         "data": f"\n[connection lost — retrying in {delay:.0f}s "
                                 f"(attempt {state.retry_attempt} of {self.retry_attempts})]\n"})

        if not await self._sleep_unless_interrupted(delay):
            return False        # aborted mid-backoff; fall through to terminate
        state.advance(ContinueReason.RETRY_TRANSIENT,
                      f"{type(error).__name__} (attempt {state.retry_attempt})")
        return True

    def _backoff_delay(self, attempt: int, hint: float | None) -> float:
        """Exponential with jitter, capped. A server's own `retry-after` wins —
        guessing 2 s against a 429 that asked for 30 just earns another 429.

        Jitter matters more than it looks: without it, several agents that hit
        the same rate limit retry in lockstep and re-collide indefinitely."""
        if hint is not None:
            return hint
        delay = min(self.retry_base_delay * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
        return delay * random.uniform(0.75, 1.25)

    async def _sleep_unless_interrupted(self, delay: float) -> bool:
        """Sleep, but stay interruptible. Returns False if the operator aborted.

        A plain sleep here would make an abort during a 30 s backoff feel like a
        hang — the one moment the loop is doing nothing is the one moment it must
        still be listening."""
        try:
            await asyncio.wait_for(self.signal.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            return True         # slept the full delay undisturbed
        return False            # the signal fired

    # ── One model turn, collected but not yet committed ──────────────────────
    async def _stream_turn(self, state: LoopState, tool_schemas: list[dict[str, Any]]) -> _Turn:
        """Stream one turn into a `_Turn`, converting a stream failure into a
        value rather than letting it unwind the loop. Deltas are emitted as they
        arrive — the operator sees the partial text either way; what a failed turn
        does not get is a place in the transcript."""
        turn = _Turn()
        text_buf: list[str] = []
        try:
            async for ev in self.model.stream(
                system=self.system_prompt,
                messages=state.messages,
                tools=tool_schemas,
                signal=self.signal,
            ):
                if isinstance(ev, TextDelta):
                    text_buf.append(ev.text)
                    await self.emit({"type": "chunk", "data": ev.text})
                elif isinstance(ev, ToolUseRequest):
                    turn.tool_uses.append(ev)
                    await self.emit({"type": "tool",
                                     "data": {"id": ev.id, "name": ev.name, "input": ev.input}})
                elif isinstance(ev, UsageReport):
                    turn.usage = ev
                elif isinstance(ev, TurnEnd):
                    turn.end = ev
        except Exception as e:  # noqa: BLE001 — classified at the failure boundary
            turn.error = e
        turn.text = "".join(text_buf)
        return turn

    # ── Tool execution: concurrency gated by declared safety (§4). ───────────
    async def _run_tools(self, tool_uses: list[ToolUseRequest]) -> list[ToolResult]:
        """Run one turn's tool batch, preserving the order the model asked for.

        Consecutive concurrency-safe calls form one group that runs together; every
        other call runs alone. Groups execute in emission order, so a read that
        follows a write in the same turn observes that write.

        Partitioning by *runs* rather than by class is the whole point: hoisting
        every safe call ahead of every unsafe one reorders across the batch, and
        because results are re-sorted into request order before they reach the
        transcript, the model has no way to detect that it read a stale file.
        """
        results: dict[str, ToolResult] = {}
        for safe, group in self._partition(tool_uses):
            if safe and len(group) > 1:
                done = await asyncio.gather(*(self._dispatch(tu) for tu in group))
                results.update({tu.id: res for tu, res in zip(group, done)})
            else:
                # Sequential — a lone call, or a mutation that may clobber Cell state.
                for tu in group:
                    results[tu.id] = await self._dispatch(tu)
        return [results[tu.id] for tu in tool_uses]

    def _partition(
        self, tool_uses: list[ToolUseRequest]
    ) -> list[tuple[bool, list[ToolUseRequest]]]:
        """Group the batch into maximal runs of consecutive concurrency-safe calls."""
        groups: list[tuple[bool, list[ToolUseRequest]]] = []
        for tu in tool_uses:
            safe = self._parallel_safe(tu)
            if safe and groups and groups[-1][0]:
                groups[-1][1].append(tu)
            else:
                groups.append((safe, [tu]))
        return groups

    def _parallel_safe(self, tu: ToolUseRequest) -> bool:
        """Whether this specific call may share a group.

        Answering needs the validated arguments, so the schema is parsed here as
        well as in dispatch. Fail closed at every step: an unknown tool, input
        the schema rejects, or a flag check that raises all mean "run it alone".
        Dispatch will produce the proper is_error a moment from now — this only
        decides company, and it is never wrong to keep bad company out."""
        tool = self.tools.get(tu.name)
        if tool is None:
            return False
        try:
            return bool(tool.is_concurrency_safe(tool.Args.model_validate(tu.input)))
        except Exception:  # noqa: BLE001 — see docstring: unsafe is the safe answer
            return False

    async def _dispatch(self, tu: ToolUseRequest) -> ToolResult:
        """One gauntlet run, bounded by the in-flight ceiling. The cap matters at
        the top of a large repo sweep — a 40-call grep batch would otherwise open
        40 simultaneous subprocesses in the Cell."""
        async with self._tool_slots:
            return await dispatch_tool(self.tools, tu.name, tu.input, self.ctx,
                                       abort=self.signal)

    # ── Terminal helpers ─────────────────────────────────────────────────────
    def _interrupted_results(self, tool_uses: list[ToolUseRequest]) -> dict[str, Any]:
        blocks = [to_anthropic_tool_result(
            tu.id, ToolResult("[interrupted before execution]", is_error=True))
            for tu in tool_uses]
        return {"role": "user", "content": blocks}

    def _note_tools(self, state: LoopState, tool_uses) -> None:
        """Record what kind of work this lap did, for the verification check.

        Only two facts matter: whether a file changed, and whether anything was
        RUN afterwards. Both are stamped with the iteration, because tests that
        passed before an edit say nothing about the edit.
        """
        for tu in tool_uses:
            if tu.name in _WRITE_TOOLS:
                state.wrote_at = state.iteration
            elif tu.name in _CHECK_TOOLS:
                state.checked_at = state.iteration
            if _persists_to_memory(tu):
                state.memory_wrote = True

    def _unverified(self, state: LoopState) -> bool:
        """Did this job change code and then stop without running anything?

        The most expensive failure in an agentic loop is not a crash — it is a
        confident report of work that was never executed. The model has no way
        to notice it skipped that step; the loop does, because it watched.

        Asked once. A second ask is nagging, and the answer to "why didn't you
        run it" is sometimes "there is nothing to run", which the loop cannot
        know but the agent can say.
        """
        return (state.wrote_at > 0
                and state.checked_at < state.wrote_at
                and not state.verification_nudged)

    def _unsaved_rule(self, state: LoopState) -> bool:
        """Did the owner state a standing rule THIS turn that was never written?

        The prompt already tells the agent to file such a rule; this is the
        backstop for the turn it does not — the failure the owner actually
        reports, an instruction obeyed once and forgotten by the next session.

        Only the CURRENT turn's message is examined (`operator_turns[-1]`), never
        the whole history: a rule the owner stated three turns ago and the agent
        saved then is not this run's business, and re-deriving it from the
        transcript every turn would nag about a rule that is already filed.

        Gated on a tool that can actually record it — without `memory` or
        `remember_about_owner` in the set there is nowhere to send the agent, and
        a nudge with no available action is the harness talking to itself. Asked
        once, like the verification nudge, for the same reason: the honest answer
        is sometimes 'that was not a standing rule', which the agent can see and
        the loop cannot."""
        if state.memory_wrote or state.rule_capture_nudged:
            return False
        if not state.operator_turns:
            return False
        if "memory" not in self.tools and "remember_about_owner" not in self.tools:
            return False
        return reminders.looks_like_standing_rule(state.operator_turns[-1])

    async def _wind_down(self, state: LoopState) -> Terminal:
        """The ceiling is reached. Ask for a handover instead of cutting the
        head off mid-thought.

        Stopping dead at the boundary throws away the one thing worth having:
        the agent knows what it just did, what is half-finished, and what it was
        about to do next, and nobody else does. Returning nothing forces the
        operator to reconstruct that by reading a scrolled-past transcript, or —
        more often — to start the job again.

        So it gets one final turn with NO tools. That is what makes this safe
        rather than a ceiling that quietly does not hold: it cannot edit another
        file, run another command, or extend the run. It can only write the
        handover. Exactly one such turn happens, because the loop is left
        immediately afterwards either way.
        """
        await self.emit({"type": "chunk", "data":
                         f"\n[reached the {self.max_iterations}-iteration ceiling "
                         "— asking for a handover]\n"})

        # An operator who already pressed ctrl+c is not waiting for a summary;
        # spending another model call on one is the wrong answer to "stop".
        if self.signal.is_set():
            return self._terminal(
                StopReason.MAX_ITERATIONS, state,
                error=f"max_iterations ({self.max_iterations}) reached")

        state.messages.append({"role": "user", "content": (
            f"You have reached this run's ceiling of {self.max_iterations} "
            "tool-using turns, so this is your last turn and you have no tools "
            "in it. Do not plan further work here — write the handover instead. "
            "State what you actually changed and where (file and line), what is "
            "half-finished and in what state it was left, and the single next "
            "step someone picking this up should take. Be specific about what "
            "you did NOT get to: an unfinished job described accurately is "
            "resumable, and one described optimistically is worse than useless."
        )})

        turn = await self._stream_turn(state, [])      # no tools: it can only talk
        if turn.error is not None:
            logger.warning("wind_down_failed", exc_info=turn.error)
            return self._terminal(
                StopReason.MAX_ITERATIONS, state,
                error=f"max_iterations ({self.max_iterations}) reached")

        if turn.usage is not None:
            state.ledger.record(turn.usage)
        if turn.text:
            state.last_text = turn.text
            state.messages.append(turn.assistant_message())

        return self._terminal(
            StopReason.MAX_ITERATIONS, state,
            error=f"max_iterations ({self.max_iterations}) reached")

    def recover_transcript(self) -> list[dict[str, Any]] | None:
        """The loop's committed transcript, still readable after a forced cancel.

        A caller that cancelled the task (rather than asking it to stop at a
        boundary) has no Terminal to read — `run_messages` unwound, and its local
        `state` died with it. This hands back whatever the loop had committed to
        the transcript before the cancel, so a force-cancelled turn loses only the
        sentence it was mid-way through streaming, not every tool round it had
        already run. None when the loop never started (the task was cancelled
        before `run_messages` assigned its state)."""
        if self._state is None:
            return None
        return list(self._state.messages)

    def _aborted(self, state: LoopState) -> Terminal:
        state.messages.append(
            {"role": "user", "content": "[the operator interrupted this run]"})
        return self._terminal(StopReason.ABORTED, state)

    def _terminal(self, reason: StopReason, state: LoopState, error: str | None = None) -> Terminal:
        return Terminal(reason=reason, final_text=state.last_text,
                        iterations=state.iteration, error=error, messages=state.messages,
                        transitions=tuple(state.transitions),
                        usage=state.ledger.snapshot())
