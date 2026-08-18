"""Around-listeners: the one thing Forge's hook seam could not express.

`warden/hooks.py` gives a plugin two places to stand — before a tool and after
it — and they are two separate calls with nothing joining them. That is enough
to inspect, veto, or rewrite. It is not enough to *wrap*, and wrapping is what
most cross-cutting behaviour actually is:

- a timeout has to arm a clock, call through, and disarm it
- a retry has to call through more than once
- a tracer has to hold a start time across the call
- a cache has to be able to not call through at all

Every one of those needs the same listener to be on both sides of the work, and
none of them can be built out of a `pre` plus a `post`. Forge's own tool deadline
is the proof: it had to be written into `dispatch_tool` itself, because there was
no seam shaped like it. DSH's `timeout-policy` is a plugin, in a plugin system
that has this, and that difference is entirely down to `next()`.

So this is the DSH `ctx.on('tools/execute', (exec, next) => …)` waterfall:
listeners compose into an onion around the real work, each free to run code
before it, after it, both, or instead of it.

**Order is registration order, outermost first.** The first listener registered
is the outermost wrapper — it sees the call first and the result last. Stated
because "first" is ambiguous for middleware and a plugin author has to know
which end they are on.

**A listener that does not call `next()` short-circuits.** Deliberate, and the
sharpest tool here: it is how a cache answers without working and how a guard
refuses without the tool ever seeing the call. It is also how a careless plugin
silently disables a tool, which is why `Waterfall.run` reports which listener
swallowed the chain when nothing reached the core.

**A listener that raises is not caught here.** Unlike the `pre_tool`/`post_tool`
hooks, which swallow plugin faults so a broken observer cannot veto work, an
around-listener owns the call: swallowing its exception would mean deciding, on
its behalf, whether the wrapped work still happened. The caller — `dispatch_tool`
— already turns any throw into an `is_error` result, which is the honest answer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, TypeVar

logger = logging.getLogger("forge.plugins")

T = TypeVar("T")

Next = Callable[[], Awaitable[Any]]
"""Call the rest of the chain. Awaiting it more than once re-runs the inner
listeners, which is what makes a retry plugin possible; not awaiting it at all
short-circuits."""

Listener = Callable[..., Awaitable[Any]]


@dataclass
class _Entry:
    plugin: str
    fn: Listener
    order: int


@dataclass
class Waterfall(Generic[T]):
    """One named extension point, with its listeners in registration order."""

    event: str
    _entries: list[_Entry] = field(default_factory=list)
    _seq: int = 0

    def on(self, plugin: str, fn: Listener) -> Callable[[], None]:
        """Register a listener. Returns its disposer.

        The disposer is the whole of unloading: a plugin's scope collects these
        and calls them, so nothing has to remember what a plugin registered or
        reason about how to undo it."""
        entry = _Entry(plugin=plugin, fn=fn, order=self._seq)
        self._seq += 1
        self._entries.append(entry)

        def _dispose() -> None:
            try:
                self._entries.remove(entry)
            except ValueError:
                pass        # already disposed; unloading twice is not an error
        return _dispose

    def __len__(self) -> int:
        return len(self._entries)

    def plugins(self) -> list[str]:
        return [e.plugin for e in self._entries]

    async def run(self, core: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Compose the listeners around `core` and run the chain.

        `core` is the real work — the actual tool call, the actual model turn.
        With no listeners registered this is `await core(*args)` plus one list
        check, which is what keeps an empty plugin set free."""
        if not self._entries:
            return await core(*args)

        reached = False

        async def _core() -> T:
            nonlocal reached
            reached = True
            return await core(*args)

        chain: Callable[[], Awaitable[T]] = _core
        # Built inside-out so that the FIRST registered listener ends up
        # outermost. Reversed here rather than at registration because the
        # readable invariant is "registration order == outermost first", and
        # keeping the list in that order is what makes `plugins()` legible.
        for entry in reversed(self._entries):
            chain = _wrap(entry, chain, args)

        result = await chain()
        if not reached:
            # Not an error — short-circuiting is a supported move — but it is
            # invisible from the outside and indistinguishable from the tool
            # having run, so it is recorded where someone debugging can find it.
            logger.info("waterfall_short_circuited",
                        extra={"event": self.event,
                               "listeners": [e.plugin for e in self._entries]})
        return result


def _wrap(entry: _Entry, inner: Callable[[], Awaitable[Any]],
          args: tuple[Any, ...]) -> Callable[[], Awaitable[Any]]:
    """One layer of the onion.

    A closure factory rather than an inline lambda in the loop above, because a
    lambda would capture `entry` by reference and every layer would end up
    running the last listener — the classic late-binding bug, and one that would
    present as "my plugin ran three times and the others never did"."""
    async def _layer() -> Any:
        return await entry.fn(*args, inner)
    return _layer


class Bus:
    """The set of named waterfalls a context exposes.

    Events are created on first use rather than declared up front. That is the
    opposite of Forge's usual posture — a closed list of seams, amended by
    review — and it is deliberate here: an event nobody listens to costs one
    dict entry, while a registry that must be edited before a plugin can define
    its own extension point puts the core in the way of every plugin that wants
    to be extended by another. The names that matter are documented in
    `forge/plugins/__init__.py`; the mechanism does not enforce them.
    """

    def __init__(self) -> None:
        self._events: dict[str, Waterfall] = {}

    def waterfall(self, event: str) -> Waterfall:
        wf = self._events.get(event)
        if wf is None:
            wf = self._events[event] = Waterfall(event=event)
        return wf

    def on(self, event: str, plugin: str, fn: Listener) -> Callable[[], None]:
        return self.waterfall(event).on(plugin, fn)

    async def run(self, event: str, core: Callable[..., Awaitable[T]], *args: Any) -> T:
        wf = self._events.get(event)
        if wf is None:
            return await core(*args)
        return await wf.run(core, *args)

    def listeners(self) -> dict[str, list[str]]:
        """Which plugins are on which event. For `/plugins` and for tests that
        need to assert a plugin actually attached rather than merely loaded."""
        return {name: wf.plugins() for name, wf in self._events.items() if len(wf)}
