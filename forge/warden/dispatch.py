"""The validate → permit → execute gauntlet (§4).

A fixed, ordered pipeline. Every failure at every stage becomes an is_error
ToolResult fed back to the model — unknown tool, bad input, permission denial,
and execution crash all look identical to the loop. No exception escapes.

Oversized results are spilled to a file in the Cell workspace and replaced with a
preview + path, so one fat result can't blow the context (§4)."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from forge.warden.hooks import run_post_tool, run_pre_tool
from forge.warden.permissions import Decision
from forge.warden.results import cap_result
from forge.warden.tool import (
    DEFAULT_TOOL_TIMEOUT_S, SELF_BOUNDED, Tool, ToolContext, ToolResult)
from forge.warden.toolerrors import format_validation_error

logger = logging.getLogger("forge.warden")

# What a denial has to say beyond "no". Without it the model is left to invent
# its own rule, and the obvious invention is the wrong one: denied `read_file`
# on a credentials path, the next thing to hand is `run_command cat` on the same
# path. That is not a workaround, it is the gate defeated by a synonym — and
# this agent has a shell, so the synonym is always available.
#
# The line worth drawing is intent, not tooling: a different route to the SAME
# permitted goal is fine, a different route to the DENIED one is not.
DENIAL_GUIDANCE = (
    "You may reach the same goal another way if that way is itself permitted — "
    "a narrower command, a different file, asking for less. You must not use a "
    "tool you still have to obtain what this denial withheld: running a shell "
    "command to read a path that was just refused defeats the gate by synonym "
    "rather than working around an obstacle, and the operator will see it as "
    "the former.\n"
    "If you genuinely cannot finish without this, stop and say so — what you "
    "were trying to do, why it needs this, and what you would do with it. The "
    "operator decides. An honest 'I am blocked on X' is a useful answer; "
    "quietly doing something adjacent and reporting success is not."
)

# The denial's counterpart, and the more dangerous half. A refusal is at least
# self-limiting: it stops one call and the model has to think again. An approval
# is the thing that generalises on its own — "they let me force-push" quietly
# becomes a standing belief about force-pushing, and the second one is never put
# to anybody.
#
# The gate already asks per action and records the EXACT action string when the
# operator says always. What it did not do is tell the model that. Said here, at
# the only moment it is concrete: attached to the call that was just approved.
APPROVAL_SCOPE = (
    "The operator approved this specific action, in this context, once. That "
    "approval does not extend to the next action of the same kind, to the same "
    "action on a different target, or to a broader version of it — a yes to one "
    "irreversible operation is not a policy about irreversible operations. If "
    "you need something like this again, ask again and let the gate stop you; "
    "do not reason from having been permitted before."
)


async def dispatch_tool(
    tools: dict[str, Tool],
    name: str,
    raw_input: dict[str, Any],
    ctx: ToolContext,
    abort: "asyncio.Event | None" = None,
) -> ToolResult:
    """Run one tool call through the full gauntlet, returning a ToolResult that is
    always safe to hand back to the model.

    `abort` is the loop's interrupt, and passing it here is what makes ctrl+c
    mean *now* rather than *at the next boundary*. Without it an interrupt is
    only noticed between tool calls, so stopping a ten-minute command took ten
    minutes — the one moment an operator most wants out is the one the
    boundaries cannot reach."""

    # 1. Resolve the tool. Unknown → correctable error, not a crash.
    tool = tools.get(name)
    if tool is None:
        available = ", ".join(sorted(tools)) or "(none)"
        return ToolResult(f"Unknown tool {name!r}. Available tools: {available}.", is_error=True)

    # 2. Validate input against the schema. A rejected call is answered with the
    #    call it should have made — see warden/toolerrors.py for why the raw
    #    Pydantic text is not an acceptable thing to hand a model.
    try:
        args = tool.Args.model_validate(raw_input)
    except ValidationError as e:
        return ToolResult(format_validation_error(name, e, tool.Args), is_error=True)

    # 3. Permit (deny → is_error; ask → put it to the operator).
    decision = ctx.permissions.resolve(tool, args, ctx)
    freshly_approved = False
    if decision.needs_ask:
        decision = await _ask(tool, name, args, decision, ctx)
        # Remembered before the decision is consumed: this is the one call the
        # operator personally cleared, and the only place the scope of that yes
        # can be stated without guessing at it later.
        freshly_approved = decision.allowed
    if not decision.allowed:
        return ToolResult(
            f"Permission denied for {name!r}: {decision.reason}\n\n{DENIAL_GUIDANCE}",
            is_error=True)
    if decision.updated_args is not None:
        try:
            args = tool.Args.model_validate(decision.updated_args)
        except ValidationError as e:
            return ToolResult(f"The permission layer rewrote {name!r}'s input into "
                              f"something invalid: {e}", is_error=True)

    # 3b. pre_tool hooks (Seam 3). Deliberately AFTER permission: a hook must not
    #     be able to see, let alone approve, what the gate refused.
    hooks = getattr(ctx, "hooks", None) or []
    if hooks:
        hooked_args, veto = await run_pre_tool(hooks, tool, args.model_dump(), ctx)
        if veto is not None:
            return ToolResult(f"A hook blocked {name!r}: {veto.reason}", is_error=True)
        try:
            args = tool.Args.model_validate(hooked_args)
        except ValidationError as e:
            # A hook rewrote the arguments into something the tool cannot accept.
            # That is the hook's bug, not the model's, so say so plainly rather
            # than handing the model a validation error it cannot act on.
            return ToolResult(f"A hook produced invalid input for {name!r}: {e}",
                              is_error=True)

    # 4. Execute, wrapped by any plugin listening on `tools/execute` and bounded
    #    by the deadline. Any throw becomes an is_error result (fail loud to the
    #    model, never out of the loop). The plugins are OUTSIDE the deadline deliberately: a
    #    listener that retries or waits is doing so on its own account, and a
    #    backstop that cut across it would make "the tool hung" and "the plugin
    #    chose to wait" indistinguishable. The core they wrap is the bounded
    #    call, so the tool itself is still never unbounded.
    limit = _deadline(tool, args, ctx)
    bus = getattr(ctx, "bus", None)

    async def _core() -> ToolResult:
        return await _call_bounded(tool, args, ctx, limit, abort)

    try:
        if bus is None:
            result = await _core()
        else:
            result = await bus.run("tools/execute", _core_with(_core), tool, args, ctx)
    except _Interrupted:
        # Not an error the model should route around — a decision. It is still
        # `is_error` because the call produced nothing usable, but the text says
        # who stopped it, so the agent reports rather than retries. The engine's
        # boundary check ends the turn a moment later either way.
        logger.info("tool_call_interrupted", extra={"tool": name})
        return ToolResult(
            f"<tool_error>INTERRUPTED: the operator stopped {name} while it was "
            f"still running.</tool_error>\n"
            f"This was a person's decision, not a failure. Do not retry it and "
            f"do not work around it. Anything {name} had already done is still "
            f"done. Stop here and report where things stand.", is_error=True)
    except _DeadlineExceeded:
        logger.warning("tool_call_timed_out", extra={"tool": name, "limit_s": limit})
        return ToolResult(_timeout_message(name, limit), is_error=True)
    except Exception as e:  # noqa: BLE001 — the loop's safety net
        logger.warning("tool_call_raised", extra={"tool": name, "error": repr(e)})
        # Say whose fault this is. A traceback class name reads to the model as
        # "you called it wrong", and the correction it invents is another call
        # with different arguments — which cannot help, because the arguments
        # already passed validation. The tool broke; the way through is a
        # different route or an honest report, not a reshaped retry.
        return ToolResult(
            f"<tool_error>{type(e).__name__}: {e}</tool_error>\n"
            f"This is a fault inside {name} itself, not a problem with your "
            f"arguments — they were valid. Retrying the same call will raise "
            f"the same way. Use a different tool or approach if one exists, and "
            f"if none does, report that {name} is broken and what you needed "
            f"from it.", is_error=True)

    # 4b. post_tool hooks (Seam 3), BEFORE capping — a redactor should act on
    #     what was produced, not on a preview of it.
    if hooks:
        result = await run_post_tool(hooks, tool, args.model_dump(), result, ctx)

    # 5. Cap result size — spill oversize to disk (§4). The batch-wide budget is
    #    the engine's job, once every result in the turn is known.
    result = await cap_result(tool, name, result, ctx)

    # 6. An approval the operator just gave says what it covers. After capping,
    #    so the scope note cannot be the part that gets truncated away.
    if freshly_approved:
        result = replace(result, content=f"{result.content}\n\n{APPROVAL_SCOPE}")
    return result


def _core_with(fn):
    """Adapt the zero-argument core to the waterfall's `core(*args)` shape.

    The listeners are handed `(tool, args, ctx, next)` because that is what a
    plugin author needs to see; the innermost call needs none of it, having
    closed over everything already. Rather than make every listener pass three
    arguments back down a chain that ignores them, the adapter swallows them
    here — which also means a listener cannot change which tool runs by
    rewriting what it forwards. Rewriting arguments is `pre_tool`'s job, where
    the result is re-validated against the schema; a silent swap at this layer
    would bypass that."""
    async def _accepts(*_ignored):
        return await fn()
    return _accepts


def _deadline(tool: Tool, args: Any, ctx: ToolContext) -> float:
    """This call's wall clock, fail-closed.

    A tool whose `timeout_s` raises does not get to run unbounded as a
    consequence: the default applies and the fault is logged. This is the same
    rule `_parallel_safe` uses in the engine — a tool that cannot answer a
    safety question gets the restrictive answer, not the permissive one."""
    try:
        return float(tool.timeout_s(args, ctx))
    except Exception as e:  # noqa: BLE001 — see docstring: the default is the safe answer
        logger.warning("tool_timeout_lookup_failed",
                       extra={"tool": tool.name, "error": repr(e)})
        return DEFAULT_TOOL_TIMEOUT_S


class _DeadlineExceeded(Exception):
    """Raised only when THIS layer's clock is the one that ran out.

    A plain `asyncio.wait_for` cannot support the message `_timeout_message`
    writes. On Python 3.11+ `asyncio.TimeoutError` is the builtin `TimeoutError`,
    so a tool that lets its OWN inner timeout escape — the graph sidecar's 30s
    read, an MCP call, an httpx deadline — is indistinguishable from the
    dispatcher's, and would be reported as "still running after 300s" when it
    actually stopped at 30. A number stated confidently and wrongly is worse
    than no number, because the model plans against it.

    So ownership is established by identity rather than by exception type, which
    is the same fix DSH's timeout-policy applies for the same reason: it checks
    that its own timer fired before it claims the timeout."""


class _Interrupted(Exception):
    """The operator stopped this call while it was still running."""


async def _call_bounded(tool: Tool, args: Any, ctx: ToolContext,
                        limit: float, abort: "asyncio.Event | None" = None) -> ToolResult:
    """`tool.call`, raced against its wall clock and the operator's interrupt.

    Three ways out, and they are kept distinct because they ask the model for
    different things:

    - **The tool finishes.** `result()` re-raises whatever it raised, including
      its own `TimeoutError`, which then lands in the dispatcher's generic
      handler and is correctly reported as a fault inside the tool rather than
      as this layer's deadline.
    - **The clock wins** → `_DeadlineExceeded`. Something is wedged.
    - **`abort` fires** → `_Interrupted`. A person decided. This is the only
      route that reaches a tool mid-call: the engine's boundary checks sit
      between batches, so without it ctrl+c during a long command was noticed
      only once that command had finished on its own.

    `SELF_BOUNDED` removes the clock but NOT the interrupt — those are different
    questions, and a tool that legitimately runs for an hour is precisely the
    one an operator most needs to be able to stop.

    Cancellation is awaited rather than fired and forgotten, so a tool holding a
    resource still runs its `finally` blocks. It does not guarantee a prompt
    release — a coroutine that swallows `CancelledError` can still stall here —
    but every tool in this repository awaits either the Cell (which kills its
    own process tree) or a socket, and both are cancellable."""
    task = asyncio.ensure_future(tool.call(args, ctx))
    waiters: set[asyncio.Future] = {task}
    watch = asyncio.ensure_future(abort.wait()) if abort is not None else None
    if watch is not None:
        waiters.add(watch)

    try:
        done, _pending = await asyncio.wait(
            waiters, timeout=None if limit == SELF_BOUNDED else limit,
            return_when=asyncio.FIRST_COMPLETED)
    finally:
        if watch is not None and not watch.done():
            watch.cancel()

    if task in done:
        return task.result()

    # Either the operator stopped it or the clock ran out. Both end the same
    # way — the task is cancelled and collected — but they are reported
    # differently, because "you stopped this" and "this hung" ask the model for
    # opposite things: one is a decision to respect, the other a fault to route
    # around.
    interrupted = watch is not None and watch in done
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # Ours, almost certainly — but not necessarily. If the operator's
        # interrupt cancelled US while we were collecting the tool's corpse,
        # that cancellation has to keep travelling: an abort reported as a
        # timeout tells the operator the harness gave up when in fact they did.
        # `cancelling()` is how a 3.11+ task tells the two apart.
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
    except Exception:  # noqa: BLE001 — it blew up on the way out; the deadline is the story
        pass
    raise _Interrupted if interrupted else _DeadlineExceeded


def _timeout_message(name: str, limit: float) -> str:
    """What the model is told when a tool hit the backstop.

    Two things it has to convey, because the wrong reading of each is the
    expensive one. First: the arguments were fine — they passed validation, and
    a model told only "timed out" reliably retries with a smaller `limit` or a
    narrower path, which cannot help and costs another full deadline. Second:
    the work may have half-happened. A cancelled `write_file` is not a no-op,
    and an agent that assumes otherwise builds on a file it never checked."""
    return (
        f"<tool_error>TOOL_TIMEOUT: {name} was still running after {limit:.0f}s "
        f"and was cancelled by the harness.</tool_error>\n"
        f"This is a wall clock the harness enforces, not a fault in your "
        f"arguments — they were valid, so retrying the same call unchanged will "
        f"most likely hit the same deadline.\n"
        f"Whatever {name} had already done before it was cancelled is still "
        f"done: check the state before you assume nothing happened. Then either "
        f"do the work in smaller pieces, use a different route to it, or — if "
        f"neither exists — report that {name} is hanging and say what you needed "
        f"from it."
    )


async def _ask(tool: Tool, name: str, args: Any, decision: "Decision",
               ctx: ToolContext) -> "Decision":
    """Put a gated action to the operator and turn the answer into a decision.

    With no oracle attached the answer is no — identical to the behaviour before
    any of this existed, which is what makes the ask safe to ship before
    anything is listening for it."""
    from forge.warden.permissions import Decision as _D

    oracle = getattr(ctx, "oracle", None)
    args_dict = args.model_dump() if hasattr(args, "model_dump") else dict(args)
    action_key = ctx.permissions._action_key(args_dict) or name

    if oracle is None:
        return _D("deny", f"{decision.reason} No operator channel is attached, so it "
                          f"cannot be approved while this job runs.", source="gate")

    try:
        answer = await oracle.ask(name, action_key, decision.reason)
    except asyncio.CancelledError:
        raise                      # an interrupt is not an answer
    except Exception as e:  # noqa: BLE001 — an oracle that broke did not say yes
        logger.warning("permission_ask_failed", extra={"tool": name, "error": repr(e)})
        return _D("deny", f"{decision.reason} The approval channel failed.", source="gate")

    if not answer.approved:
        return _D("deny", f"The operator declined this: {answer.note or decision.reason}",
                  source="gate")

    if answer.remember:
        # Records the EXACT action, never a pattern. A glob is a deliberate,
        # hand-written act by an operator reading their own allow-list file; it
        # is not something to infer from one click on one command.
        ctx.permissions.allowlist.add(f"{name}:{action_key}")
    return _D("allow", "approved by the operator", source="gate")


def to_anthropic_tool_result(tool_use_id: str, result: ToolResult) -> dict[str, Any]:
    """Render a ToolResult as an Anthropic tool_result content block."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
    }


def _debug_dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)[:500]
    except Exception:  # noqa: BLE001
        return repr(obj)[:500]
