"""Asking the operator a question, as distinct from asking permission.

The whole design turns on one asymmetry, and these tests exist to pin it:

    an unanswered PERMISSION request is a NO      — absence must not become consent
    an unanswered QUESTION is not a no            — there is nothing to refuse

Getting the second one wrong the way the first one is right would mean a server
job that asks "REST or WebSocket?" at 3am blocks until it times out and then
treats silence as refusal. There is no refusal available. It should decide and
disclose.
"""
from __future__ import annotations

import asyncio

from forge.tools.ask import AskOperator
from forge.warden.oracle import (UNANSWERED, AutoDenyOracle, ChannelOracle,
                                 Reply, has_consult)


class _Ctx:
    def __init__(self, oracle=None) -> None:
        self.oracle = oracle


def _ask(ctx, question="REST or WebSocket?", options=None):
    tool = AskOperator()
    args = {"question": question, "options": options or []}
    return asyncio.run(tool.call(tool.Args.model_validate(args), ctx))


# ── The asymmetry ────────────────────────────────────────────────────────────

def test_no_answer_is_not_a_refusal():
    """The failure direction that makes this different from the gate."""
    out = _ask(_Ctx(AutoDenyOracle()))

    assert not out.is_error, "an unanswered question is not an error"
    assert "decide" in out.content.lower() or "judge best" in out.content.lower()
    assert "report" in out.content.lower(), "and it must disclose that it decided"


def test_permission_still_denies_when_unanswered():
    """The other half of the pair, unchanged. If this ever stops holding, the
    asymmetry has been flattened in the wrong direction."""
    answer = asyncio.run(AutoDenyOracle().ask("run_command", "rm -rf /", "destructive"))
    assert answer.approved is False


def test_the_guidance_tells_it_not_to_ask_again():
    """A model that reads "no answer" as "try again" turns one unanswered
    question into a loop against a person who is asleep."""
    assert "do not ask again" in UNANSWERED.guidance.lower()
    assert "do not wait" in UNANSWERED.guidance.lower()


def test_an_answer_passes_through_verbatim():
    assert Reply(answered=True, text="use WebSocket").guidance == "use WebSocket"


# ── Over the channel ─────────────────────────────────────────────────────────

def _oracle(timeout=5.0):
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    return ChannelOracle(send, timeout_s=timeout), sent


def test_a_question_is_its_own_frame_type():
    """Not a flag on permission_request: a consumer rendering Allow/Deny would
    show the wrong two buttons and have nowhere to put prose."""
    oracle, sent = _oracle(timeout=0.05)
    asyncio.run(oracle.consult("which approach?", ["a", "b"]))

    assert sent[0]["type"] == "question"
    assert sent[0]["question"] == "which approach?"
    assert sent[0]["options"] == ["a", "b"]
    assert "ask_id" in sent[0]


def test_an_answer_resolves_the_parked_question():
    async def scenario():
        oracle, sent = _oracle()
        task = asyncio.create_task(oracle.consult("which?"))
        await asyncio.sleep(0)
        oracle.answer(sent[0]["ask_id"], "the second one")
        return await task

    reply = asyncio.run(scenario())
    assert reply.answered is True and reply.text == "the second one"


def test_a_timeout_is_unanswered_not_denied():
    oracle, _ = _oracle(timeout=0.05)
    assert asyncio.run(oracle.consult("which?")) == UNANSWERED


def test_a_late_answer_is_dropped_quietly():
    """A slow operator is not a bug."""
    oracle, _ = _oracle()
    assert oracle.answer("never-asked", "hello") is False


def test_a_closed_socket_leaves_questions_unanswered_not_refused():
    """abandon_all resolves permissions to DENIED. Questions must not follow it
    there — nobody being present is not the owner saying no to an idea."""
    async def scenario():
        oracle, sent = _oracle()
        task = asyncio.create_task(oracle.consult("which?"))
        await asyncio.sleep(0)
        oracle.abandon_all("channel closed")
        return await task

    assert asyncio.run(scenario()).answered is False


def test_an_unsendable_question_does_not_raise():
    async def boom(_frame):
        raise ConnectionError("socket gone")

    oracle = ChannelOracle(boom, timeout_s=1.0)
    assert asyncio.run(oracle.consult("which?")) == UNANSWERED


# ── The tool ─────────────────────────────────────────────────────────────────

def test_no_oracle_at_all_is_handled():
    out = _ask(_Ctx(None))
    assert out.is_error and "Do not retry" in out.content


def test_an_oracle_without_consult_is_detected():
    """Oracles arrive through a seam; an external one may predate `consult`.
    Better than an AttributeError inside a dispatch, which reads to the model as
    the question having failed rather than as unsupported."""
    class Old:
        async def ask(self, *_a):
            ...

    assert has_consult(Old()) is False
    assert _ask(_Ctx(Old())).is_error


def test_an_empty_question_is_refused():
    out = _ask(_Ctx(AutoDenyOracle()), question="   ")
    assert out.is_error


def test_the_answer_reaches_the_model():
    class Helpful:
        async def consult(self, question, options=None):
            return Reply(answered=True, text="go with WebSocket")

    out = _ask(_Ctx(Helpful()))
    assert not out.is_error and out.content == "go with WebSocket"


def test_asking_is_read_only_so_plan_mode_allows_it():
    """Asking what to build is exactly what a review pass is for."""
    assert AskOperator.READ_ONLY is True


def test_questions_are_never_batched():
    """Two arriving at once is the shape that trains an owner to stop reading."""
    assert AskOperator.CONCURRENCY_SAFE is False


def test_the_description_rations_it():
    text = AskOperator.description
    assert "Do NOT" in text
    assert "permission" in text, "it must say the gate handles permission itself"
