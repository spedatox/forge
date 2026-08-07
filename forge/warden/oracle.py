"""Seam 2 — asking the operator (H7).

The safety gate used to be a wall: a gated-but-intended operation returned
"the operator must allow-list it explicitly" and there was no way to do that
mid-job. The job dead-ended on an action the operator would have approved in two
seconds.

The gate is now a checkpoint. What it never becomes is *silent*: every gate match
still refuses to proceed without an explicit yes, and an ask with no answer
degrades to deny. The failure direction is fixed — an unanswered question is a
no, a lost socket is a no, a timeout is a no. Nothing here can turn absence of
an operator into permission.

Two implementations, chosen at assembly. That is the whole shape of a seam: one
protocol, more than one answer, no core module knowing which.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger("forge.warden")

DEFAULT_ASK_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Answer:
    approved: bool
    remember: bool = False
    note: str = ""


DENIED = Answer(approved=False)


@dataclass(frozen=True)
class Reply:
    """The operator's answer to an open question.

    Distinct from `Answer` because the two failure directions are opposites, and
    conflating them would get one of them wrong.

    An unanswered PERMISSION request is a no: absence of an operator must never
    become consent, so `Answer` degrades to `approved=False` and the action does
    not happen. An unanswered QUESTION is not a no — there is nothing to refuse.
    Blocking the work because nobody was around to offer an opinion would turn a
    courtesy into a deadlock, so this degrades to `answered=False` and the agent
    is told to use its own judgement and say what it assumed.
    """
    answered: bool
    text: str = ""

    @property
    def guidance(self) -> str:
        """What the model should be told, either way."""
        if self.answered:
            return self.text
        return ("No answer came back — nobody is reachable right now. Do not "
                "wait and do not ask again. Choose the option you judge best, "
                "proceed, and state plainly in your final report which way you "
                "went and that you decided it yourself.")


UNANSWERED = Reply(answered=False)


@runtime_checkable
class PermissionOracle(Protocol):
    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        """Put one decision to whoever can make it. Must always return."""
        ...

    async def consult(self, question: str, options: list[str] | None = None) -> Reply:
        """Put an open question to the operator. Must always return.

        Optional on the protocol in practice — `has_consult` below lets callers
        detect an older oracle rather than crashing on one. Every implementation
        in this repo has it."""
        ...


def has_consult(oracle: Any) -> bool:
    """Whether this oracle can carry an open question.

    Oracles arrive through a seam and an external one may predate `consult`.
    Asking here beats an AttributeError inside a tool dispatch, which the model
    would read as the question having failed rather than as unsupported."""
    return callable(getattr(oracle, "consult", None))


class AutoDenyOracle:
    """No operator is reachable, so the answer is no.

    Used by `serve` mode without a channel, the offline demo, tests, and as the
    fallback wherever a real oracle could not be built. It is not a degraded
    mode — it is Mark I's exact behaviour, which is why shipping the ask before
    any counterpart exists is safe."""

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        return Answer(approved=False,
                      note="no operator channel is attached to this job, so gated "
                           "operations cannot be approved while it runs")

    async def consult(self, question: str, options: list[str] | None = None) -> Reply:
        """No channel, so no answer — but note the asymmetry with `ask` above.

        That one denies. This one does not "deny" anything, because a question
        is not a request for permission: it returns unanswered and the model is
        told to decide for itself and disclose that it did."""
        return UNANSWERED


class ChannelOracle:
    """Sends the question somewhere and parks until an answer comes back.

    The park happens *inside one tool dispatch*, which is what keeps this from
    touching anything else: interrupt boundaries, transcript shape and batch
    semantics all hold, because from the loop's perspective one tool simply took
    a while. A parked unsafe tool blocks the rest of its sequential batch by
    design — you do not run the next mutation while the operator is still
    thinking about this one."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 timeout_s: float = DEFAULT_ASK_TIMEOUT_S,
                 job_id: str = "", chat_id: str | None = None) -> None:
        self._send = send
        self._timeout = timeout_s
        self._job_id = job_id
        self._chat_id = chat_id
        self._pending: dict[str, asyncio.Future[Answer]] = {}
        self._questions: dict[str, asyncio.Future[Reply]] = {}

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        ask_id = uuid.uuid4().hex
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending[ask_id] = future
        frame = {
            "type": "permission_request",
            "ask_id": ask_id,
            "job_id": self._job_id,
            "tool": tool_name,
            "action_key": action_key,
            "reason": reason,
            "timeout_s": self._timeout,
        }
        if self._chat_id:
            frame["chat_id"] = self._chat_id
        try:
            await self._send(frame)
        except Exception as e:  # noqa: BLE001 — an unsendable question is unanswered
            logger.warning("permission_request_send_failed", extra={"error": repr(e)})
            self._pending.pop(ask_id, None)
            return Answer(False, note="the operator channel could not be reached")

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.info("permission_request_timed_out", extra={"ask_id": ask_id})
            return Answer(False, note=f"no answer within {self._timeout:.0f}s")
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(ask_id, None)

    async def consult(self, question: str, options: list[str] | None = None) -> Reply:
        """Park on an open question, the same way `ask` parks on a decision.

        A separate frame type rather than a flag on `permission_request`: a
        consumer that renders every ask as an Allow/Deny pair would show the
        wrong two buttons for "which of these three approaches?", and would have
        no way to send prose back. Different question, different frame.

        The timeout is the same clock but the expiry means something else — see
        `Reply`. Here, running out means proceed on your own judgement.
        """
        ask_id = uuid.uuid4().hex
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._questions[ask_id] = future
        frame = {
            "type": "question",
            "ask_id": ask_id,
            "job_id": self._job_id,
            "question": question,
            "options": list(options or []),
            "timeout_s": self._timeout,
        }
        if self._chat_id:
            frame["chat_id"] = self._chat_id
        try:
            await self._send(frame)
        except Exception as e:  # noqa: BLE001
            logger.warning("question_send_failed", extra={"error": repr(e)})
            self._questions.pop(ask_id, None)
            return UNANSWERED

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.info("question_timed_out", extra={"ask_id": ask_id})
            return UNANSWERED
        except asyncio.CancelledError:
            raise
        finally:
            self._questions.pop(ask_id, None)

    def answer(self, ask_id: str, text: str) -> bool:
        """Deliver a reply to a parked question. False if nothing was waiting."""
        future = self._questions.pop(ask_id, None)
        if future is None or future.done():
            return False
        future.set_result(Reply(answered=True, text=text))
        return True

    def resolve(self, ask_id: str, answer: Answer) -> bool:
        """Deliver an answer. False if nothing was waiting for it — a late reply
        to a question that already timed out is not an error, and treating it as
        one would make a slow operator look like a bug."""
        future = self._pending.pop(ask_id, None)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def abandon_all(self, note: str = "the operator channel closed") -> None:
        """Resolve every parked question to denied. Called on socket teardown:
        losing the channel is not a reason to hang, and it is certainly not a
        reason to proceed."""
        for ask_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(Answer(False, note=note))
            self._pending.pop(ask_id, None)
        # Questions abandon to UNANSWERED, not to a denial. A closed socket is
        # not the operator saying no to an idea — it is nobody being there, and
        # the model should decide and disclose rather than treat it as refusal.
        for ask_id, q in list(self._questions.items()):
            if not q.done():
                q.set_result(UNANSWERED)
            self._questions.pop(ask_id, None)
