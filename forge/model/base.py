"""The Model protocol and its streamed event types.

Kept intentionally tiny: a model streams text deltas and tool-use requests for
one turn, then the generator ends. The engine turns that into an assistant
message and decides — solely from whether any tool-use requests arrived —
whether to loop again (§3)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseRequest:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    reasoning_content: str | None = None
    """Provider reasoning text that preceded the tool call (DeepSeek reasoning
    models). Carried here so the assistant message carrying the tool call can
    round-trip it back, which DeepSeek requires once thinking is enabled. Empty
    for providers that do not emit it."""


@dataclass
class UsageReport:
    """What one turn cost, yielded once after content.

    **Optional by contract.** A model that never yields this still works — the
    ledger falls back to a character estimate. That tolerance is what keeps a
    third-party provider cheap to add: reporting usage is a capability, not an
    obligation. `estimated` marks figures that were guessed rather than
    reported, so nothing downstream renders a guess as a measurement."""
    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0
    estimated: bool = False


@dataclass
class TurnEnd:
    """Why the turn stopped, yielded last when the provider says.

    **Absence means unknown, which is not the same as "it finished."** Nothing
    downstream may read a missing TurnEnd as a clean end — a turn cut off at the
    output cap looks exactly like a completed one from the transcript alone
    (text, no tool-use blocks), so a harness that cannot tell them apart reports
    half a sentence as the answer.

    This is a separate event rather than a field on `UsageReport` on purpose.
    `UsageReport` is optional by contract, and hanging the truncation signal off
    an optional event fails open on precisely the providers least likely to
    implement it — the wrong direction for the one signal here that can corrupt
    work rather than waste a call.

    Two sources, in the same precedence `model/errors.py` uses for classifying
    failures: the provider's own word first, an inference second. `reason` is
    what the provider said; `truncated_estimate` is set when it said nothing but
    the turn's output tokens reached the cap, which is a reliable-enough tell.
    Marked separately so nothing renders a guess as a measurement."""
    reason: str | None = None
    """Normalized: "end_turn", "tool_use", "max_tokens", or the provider's own
    string when it does not map. None when the provider reported nothing."""

    truncated_estimate: bool = False
    """Inferred from output tokens reaching the configured cap."""

    def truncated(self) -> bool:
        return self.reason == "max_tokens" or self.truncated_estimate


ModelEvent = TextDelta | ToolUseRequest | UsageReport | TurnEnd


@runtime_checkable
class Model(Protocol):
    model_id: str

    def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        signal: asyncio.Event,
    ) -> AsyncIterator[ModelEvent]:
        """Stream one assistant turn. Yields TextDelta and ToolUseRequest events,
        optionally a closing UsageReport and TurnEnd; the generator ending marks
        turn completion. Must honor `signal` by stopping promptly when it is set.

        A provider that can report why the turn ended SHOULD yield TurnEnd — it
        is the only way the loop can distinguish a finished turn from one cut
        off at the output cap. Omitting it is permitted and costs that
        distinction."""
        ...
