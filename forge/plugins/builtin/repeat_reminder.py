"""Break a loop the model cannot see itself in — a port of DSH's guard.

Forge already had repeat detection in `warden/reminders.py`. This replaces it on
three counts, each of which was a real blind spot:

**It counts successes.** The old rule fired only on repeated *failures*, and
defended that: "a tool called twice the same way that worked twice is a loop
doing its job." True of a `grep`. False of a model that has read the same file
eight times, or re-run the same passing test after every edit without changing
anything — both of which succeed every time and get nowhere.

**It cannot be laundered.** The old counter reset on ANY successful call in the
batch, so `grep X (fail) → todo_write (ok) → grep X (fail)` never tripped. DSH
names this exactly: bookkeeping tools interleaved into a loop must not launder
it. Untracked calls here are *transparent* — they neither count nor reset.

**It escalates.** The old rule fired once per job, on the theory that a repeated
reminder becomes wallpaper. Right for judgement-shaped nudges ("you have not run
the tests"); wrong for a counter, where the fifth occurrence is new information
the third did not carry.

It is advisory by construction — it never vetoes and never rewrites. A
legitimately repeated call is delayed by nothing. The decision to change
approach or stop stays with the model, which is the only place it can sensibly
live: the harness can see the repetition but not whether it is pointless.

Written as a plugin rather than as more of `reminders.py` because it is the
proof the plugin system carries real behaviour. It reaches nothing the core had
to expose for it: one `tools/execute` listener and the existing context.

**It does not switch the old rule off**, and deliberately does not try to: a
plugin reaching into `warden/reminders.py` to silence a core rule is exactly the
coupling this architecture exists to avoid. They overlap in one narrow case — a
call repeated and failing — where an operator running both may see two nudges in
one turn. If that bothers you, drop `repeated_failure` from `reminders._RULES`;
everything this guard does is a superset of it.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, field_validator

name = "repeat-tool-reminder"


class Config(BaseModel):
    thresholds: list[int] = [3, 5, 8]
    """Run lengths that earn a reminder. The FIRST is a short generic nudge;
    later ones name the tool, the count and the arguments."""

    include: list[str] = []
    """Tool-name globs to track. Empty means every tool."""

    exclude: list[str] = ["todo_write"]
    """Globs transparent to the chain — neither counting nor resetting."""

    preview_chars: int = 500
    """Cap on arguments quoted in the detailed reminder. Bounds the REMINDER
    only: the chain key always compares the full canonical string, so a looping
    `write_file` cannot ride its payload into the next request, and capping the
    key instead would make two different large writes look identical."""

    @field_validator("thresholds")
    @classmethod
    def _sane(cls, v: list[int]) -> list[int]:
        """Fails loud, never falls back.

        A silent default here discards the operator's intent without telling
        them — they set `thresholds: [0]` meaning something, got `[3, 5, 8]`,
        and have no way to discover the difference. Values are validated;
        referents deliberately are not (see `include`/`exclude`, where a pattern
        matching no live tool is valid and must stay valid across deployments)."""
        if not v:
            raise ValueError("must not be empty")
        if any(int(t) < 2 for t in v):
            raise ValueError("every threshold must be >= 2 (a run of 1 is a first call)")
        if len(set(v)) != len(v):
            raise ValueError(f"duplicate thresholds in {v}")
        return sorted(v)

    @field_validator("preview_chars")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v


_GENTLE = (
    "You have now made this exact call, with these exact arguments, several "
    "times in a row. Read the last result again before repeating it: if it did "
    "not get you what you needed, the same call will not either. Change the "
    "arguments, change the approach, or stop and say what you are stuck on."
)


def _detailed(tool: str, count: int, args_preview: str) -> str:
    return (
        f"Repeated tool call detected:\n"
        f"  tool: {tool}\n"
        f"  consecutive identical calls: {count}\n"
        f"  arguments: {args_preview}\n"
        f"These calls are not making progress. Do not make this call again with "
        f"these arguments. Either act on what the last result actually said, try "
        f"a genuinely different route, or stop and report what you could not do "
        f"and why — an honest dead end is worth more than a ninth attempt."
    )


def _canonical(args: Any) -> str:
    """Deep key-sorted JSON, so argument order is not identity.

    `{"path": "a", "limit": 5}` and `{"limit": 5, "path": "a"}` are the same
    call. A model that reorders its own arguments between attempts is still
    looping, and comparing raw dicts would miss it."""
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


@dataclass
class _Chain:
    key: str = ""
    count: int = 0
    fired: set[int] = field(default_factory=set)
    """Thresholds already delivered for THIS run. A run that passes 3 and then
    breaks and re-forms starts clean, because the chain is reset; without this,
    a run sitting at exactly 3 across a retry would re-fire the same reminder."""


def apply(ctx, config: Config) -> None:
    thresholds = set(config.thresholds)
    first = config.thresholds[0]
    chain = _Chain()

    def tracked(tool_name: str) -> bool:
        if config.include and not any(fnmatch.fnmatch(tool_name, p) for p in config.include):
            return False
        return not any(fnmatch.fnmatch(tool_name, p) for p in config.exclude)

    def observe(tool_name: str, args: Any) -> str | None:
        """Advance the chain and return the reminder this attempt earned."""
        key = f"{tool_name}\x00{_canonical(args)}"
        if key == chain.key:
            chain.count += 1
        else:
            chain.key, chain.count, chain.fired = key, 1, set()
        if chain.count not in thresholds or chain.count in chain.fired:
            return None
        chain.fired.add(chain.count)
        if chain.count == first:
            return _GENTLE
        preview = _canonical(args)
        if len(preview) > config.preview_chars:
            cut = len(preview) - config.preview_chars
            preview = f"{preview[:config.preview_chars]}…[{cut} more chars]"
        return _detailed(tool_name, chain.count, preview)

    async def watch(tool, args, _tool_ctx, next):
        """Count first, then delegate, then append.

        Counting BEFORE `next()` is what makes a denied or failed call count —
        a model hammering a call the gate keeps refusing is exactly the loop
        worth breaking, and counting after would skip every one of them.

        The reminder is appended to whatever comes back rather than replacing
        it, and appended for errors too: the result the model needs to re-read
        is usually the error itself."""
        if not tracked(getattr(tool, "name", "")):
            return await next()

        payload = args.model_dump() if hasattr(args, "model_dump") else args
        reminder = observe(getattr(tool, "name", "?"), payload)
        result = await next()
        if reminder is None:
            return result
        from dataclasses import replace as _replace
        return _replace(result,
                        content=f"{result.content}\n\n<system-reminder>\n"
                                f"{reminder}\n</system-reminder>")

    ctx.on("tools/execute", watch)
