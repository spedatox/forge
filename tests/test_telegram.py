"""Outbound Telegram — the surface for an owner who is not at the terminal.

Two things carry the weight here. The policy is INHERITED from
`tui/notify.MIN_SECONDS` rather than restated, so the bell and the phone cannot
disagree about whether a job was long enough to mention. And Forge only ever
sends: Mark VI owns the inbound side, and a second poller would race it for the
same updates.
"""
from __future__ import annotations

import asyncio

import pytest

from forge import notify
from forge.tools.telegram import TelegramSend
from forge.tui.notify import MIN_SECONDS
from forge.warden.toolsource import resolve_optional


class _Response:
    def __init__(self, status=200) -> None:
        self.status_code = status


class _FakeHttpx:
    def __init__(self, response=None, boom: Exception | None = None) -> None:
        self.response = response or _Response()
        self.boom = boom
        self.posts: list[dict] = []

    def AsyncClient(self, **_kw):  # noqa: N802 — mirrors httpx
        outer = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, url, **kwargs):
                outer.posts.append({"url": url, **kwargs})
                if outer.boom:
                    raise outer.boom
                return outer.response

        return _C()


@pytest.fixture
def wired(monkeypatch):
    fake = _FakeHttpx()
    monkeypatch.setattr(notify, "_client", lambda: fake)
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN", "bot-token")
    monkeypatch.setenv("FORGE_TELEGRAM_CHAT_ID", "4242")
    monkeypatch.delenv("FORGE_NO_TELEGRAM", raising=False)
    return fake


# ── Configuration ────────────────────────────────────────────────────────────

def test_unconfigured_is_silent(monkeypatch):
    monkeypatch.delenv("FORGE_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_TELEGRAM_CHAT_ID", raising=False)
    assert notify.configured() is False
    assert asyncio.run(notify.send("hello")) is False


def test_a_token_without_a_chat_id_is_not_configured(monkeypatch):
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN", "t")
    monkeypatch.delenv("FORGE_TELEGRAM_CHAT_ID", raising=False)
    assert notify.configured() is False


def test_the_off_switch_wins(monkeypatch, wired):
    monkeypatch.setenv("FORGE_NO_TELEGRAM", "1")
    assert notify.configured() is False


def test_a_per_agent_token_beats_the_shared_one(monkeypatch):
    """One bot per agent, so the owner's contact list is the agent roster."""
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN", "shared")
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN_CENTURION", "centurion-bot")
    assert notify.token_for("centurion") == "centurion-bot"
    assert notify.token_for("optimus") == "shared"


def test_the_tool_is_withheld_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FORGE_TELEGRAM_TOKEN", raising=False)
    tools = {"grep": object(), "telegram_send": object()}
    assert set(resolve_optional(tools)) == {"grep"}


# ── Sending ──────────────────────────────────────────────────────────────────

def test_a_message_reaches_sendmessage(wired):
    assert asyncio.run(notify.send("done")) is True
    assert wired.posts[0]["url"].endswith("/botbot-token/sendMessage")
    assert wired.posts[0]["json"]["chat_id"] == "4242"
    assert wired.posts[0]["json"]["text"] == "done"


def test_an_empty_message_is_not_sent(wired):
    assert asyncio.run(notify.send("   ")) is False
    assert wired.posts == []


def test_forge_never_polls_for_updates(wired):
    """Mark VI owns the inbound side. A second poller races it for the same
    update stream and messages get delivered twice or stolen."""
    asyncio.run(notify.send("x"))
    assert not any("getUpdates" in p["url"] or "setWebhook" in p["url"]
                   for p in wired.posts)


def test_a_failed_send_is_not_an_exception(monkeypatch):
    monkeypatch.setattr(notify, "_client",
                        lambda: _FakeHttpx(boom=ConnectionError("down")))
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("FORGE_TELEGRAM_CHAT_ID", "1")
    assert asyncio.run(notify.send("x")) is False


def test_a_non_200_stops_rather_than_continuing(monkeypatch):
    monkeypatch.setattr(notify, "_client", lambda: _FakeHttpx(_Response(403)))
    monkeypatch.setenv("FORGE_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("FORGE_TELEGRAM_CHAT_ID", "1")
    assert asyncio.run(notify.send("x")) is False


# ── Chunking ─────────────────────────────────────────────────────────────────

def test_a_short_message_is_one_chunk():
    assert notify.chunks("hello") == ["hello"]


def test_an_oversized_message_is_split_under_the_ceiling():
    parts = notify.chunks("x" * 10_000)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)


def test_the_split_prefers_a_paragraph_boundary():
    """A hard slice at 4096 lands mid-word. Looking back for a blank line costs
    nothing and makes the seam invisible."""
    body = ("a" * 3_900) + "\n\n" + ("b" * 3_000)
    parts = notify.chunks(body)
    assert parts[0].endswith("a")
    assert parts[1].startswith("b")


def test_a_long_message_is_sent_as_several(wired):
    asyncio.run(notify.send("y" * 9_000))
    assert len(wired.posts) == 3


# ── The completion notice ────────────────────────────────────────────────────

def test_a_short_job_is_not_announced(wired):
    """Inherited from tui/notify.MIN_SECONDS — the bell and the phone must not
    disagree about the same job."""
    assert asyncio.run(notify.job_finished("optimus", "t", "s", 5.0)) is False
    assert wired.posts == []


def test_a_long_job_is_announced(wired):
    assert asyncio.run(
        notify.job_finished("optimus", "prototype the thing", "done", MIN_SECONDS + 1)) is True
    text = wired.posts[0]["json"]["text"]
    assert "optimus" in text
    assert "prototype the thing" in text


def test_the_task_comes_before_the_outcome(wired):
    """After an hour away the owner needs to know WHICH job finished before
    anything about how it went."""
    asyncio.run(notify.job_finished("optimus", "THE-TASK", "THE-SUMMARY", 600))
    text = wired.posts[0]["json"]["text"]
    assert text.index("THE-TASK") < text.index("THE-SUMMARY")


def test_a_failed_job_is_marked_differently(wired):
    asyncio.run(notify.job_finished("optimus", "t", "s", 600, ok=False))
    assert "⚠️" in wired.posts[0]["json"]["text"]


# ── The tool ─────────────────────────────────────────────────────────────────

class _Ctx:
    agent_id = "optimus"


def _send(message: str):
    tool = TelegramSend()
    return asyncio.run(tool.call(tool.Args.model_validate({"message": message}), _Ctx()))


def test_the_tool_sends(wired):
    out = _send("blocked on a decision")
    assert not out.is_error
    assert wired.posts[0]["json"]["text"] == "blocked on a decision"


def test_the_tool_explains_itself_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FORGE_TELEGRAM_TOKEN", raising=False)
    out = _send("hi")
    assert out.is_error
    assert "do not retry" in out.content.lower()
    assert "final report" in out.content


def test_an_overlong_message_is_truncated_not_refused(wired):
    """The model already decided this was worth interrupting a person for.
    Losing it entirely over its length serves nobody."""
    out = _send("z" * 5_000)
    assert not out.is_error
    assert "truncated" in wired.posts[0]["json"]["text"]


def test_the_tool_is_not_read_only():
    """It leaves a mark outside the workspace, which is what makes plan mode
    deny it — a review pass must not message anybody."""
    assert TelegramSend.READ_ONLY is False


def test_the_description_discourages_narration():
    assert "Do NOT" in TelegramSend.description
    assert "cannot be unsent" in TelegramSend.description
