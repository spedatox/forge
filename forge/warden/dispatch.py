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
from forge.warden.tool import Tool, ToolContext, ToolResult
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
) -> ToolResult:
    """Run one tool call through the full gauntlet, returning a ToolResult that is
    always safe to hand back to the model."""

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

    # 4. Execute. Any throw becomes an is_error result (fail loud to the model,
    #    never out of the loop).
    try:
        result = await tool.call(args, ctx)
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
