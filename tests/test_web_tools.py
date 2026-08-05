"""web_search / web_fetch.

The behaviour that matters here is the failure behaviour. A research tool is
called mid-debug, on a turn that already has work in flight, and every way it
can fail — no key, no client, a 500, a timeout, a JavaScript-only page — has to
come back as a readable is_error the model can route around. One raise kills
the turn, and the operator sees a dead agent instead of "I couldn't reach the
docs, here's what the repo says".
"""
from __future__ import annotations

import asyncio
import json

import pytest

from forge.tools import ALL_TOOLS, CODING_TOOLS, WEB_TOOLS
from forge.tools.web import WebFetch, WebFetchArgs, WebSearch, WebSearchArgs, _readable
from forge.warden.tool import ToolContext


class _Response:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        if self._raises:
            raise self._raises
        return self._response

    async def get(self, *a, **kw):
        if self._raises:
            raise self._raises
        return self._response


def _httpx(response=None, raises=None):
    class _Module:
        @staticmethod
        def AsyncClient(**kw):
            return _Client(response, raises)

    return _Module


def _run(tool, args, ctx):
    """Forge tests drive async tools with asyncio.run rather than a plugin."""
    return asyncio.run(tool.call(args, ctx))


def _ctx(network_allowed: bool = False) -> ToolContext:
    """Cell network OFF by default — the point is that it does not gate these."""
    return ToolContext(
        agent_id="optimus", cell=None, graph=None, files=None,
        permissions=None, network_allowed=network_allowed,
    )


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


# ── Wiring ──────────────────────────────────────────────────────────────────


def test_web_tools_are_registered():
    assert "web_search" in ALL_TOOLS
    assert "web_fetch" in ALL_TOOLS
    assert [c.name for c in WEB_TOOLS] == ["web_search", "web_fetch"]


def test_web_is_not_bundled_into_coding():
    """A profile takes navigation without taking the open internet."""
    coding = {c.name for c in CODING_TOOLS}
    assert "web_search" not in coding and "web_fetch" not in coding


def test_optimus_has_web_but_centurion_does_not():
    """Research is Optimus's job. Centurion's allowlist is its own boundary and
    gaining the open internet must be a deliberate edit to its profile, not a
    side effect of adding a tool group."""
    from forge.agents.registry import AgentRegistry

    registry = AgentRegistry.load()
    assert "web_search" in registry.get("optimus").tool_names
    assert "web_fetch" in registry.get("optimus").tool_names
    assert "web_search" not in registry.get("centurion").tool_names


def test_both_declare_read_only_and_concurrency_safe():
    """Several lookups at once is the normal shape of research."""
    for tool, args in ((WebSearch(), WebSearchArgs(query="x")),
                       (WebFetch(), WebFetchArgs(url="https://x.dev"))):
        assert tool.is_read_only(args) is True
        assert tool.is_concurrency_safe(args) is True
        assert tool.is_destructive(args) is False


# ── The Cell's network posture must not gate the harness ────────────────────


def test_search_works_while_the_cell_is_airgapped(monkeypatch, key):
    """allow_network=false governs code the model RUNS, not the agent's reading."""
    payload = {"results": [{"title": "T", "url": "https://d.dev", "content": "c"}]}
    monkeypatch.setattr("forge.tools.web._client", lambda: _httpx(_Response(payload=payload)))

    result = _run(WebSearch(), WebSearchArgs(query="asyncio"), _ctx(network_allowed=False))

    assert not result.is_error
    assert "https://d.dev" in result.content


# ── Failure behaviour ───────────────────────────────────────────────────────


def test_missing_key_is_an_explained_error(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr("forge.tools.web._client", lambda: _httpx(_Response(payload={})))

    result = _run(WebSearch(), WebSearchArgs(query="x"), _ctx())

    assert result.is_error
    assert "TAVILY_API_KEY" in result.content
    assert "Do not retry" in result.content


def test_missing_http_client_is_an_explained_error(monkeypatch, key):
    monkeypatch.setattr("forge.tools.web._client", lambda: None)

    result = _run(WebSearch(), WebSearchArgs(query="x"), _ctx())

    assert result.is_error and "httpx" in result.content


def test_provider_outage_does_not_raise(monkeypatch, key):
    monkeypatch.setattr("forge.tools.web._client",
                        lambda: _httpx(raises=TimeoutError("timed out")))

    result = _run(WebSearch(), WebSearchArgs(query="x"), _ctx())

    assert result.is_error and "timed out" in result.content


def test_http_error_is_reported_not_raised(monkeypatch, key):
    monkeypatch.setattr("forge.tools.web._client",
                        lambda: _httpx(_Response(status=503, text="upstream down")))

    result = _run(WebSearch(), WebSearchArgs(query="x"), _ctx())

    assert result.is_error and "503" in result.content


def test_empty_results_is_not_an_error(monkeypatch, key):
    """Nothing found is an answer, not a failure — is_error would make the model retry."""
    monkeypatch.setattr("forge.tools.web._client",
                        lambda: _httpx(_Response(payload={"results": []})))

    result = _run(WebSearch(), WebSearchArgs(query="zzz"), _ctx())

    assert not result.is_error
    assert "No results" in result.content


def test_the_synthesised_answer_is_labelled_unverified(monkeypatch, key):
    payload = {"answer": "Use TaskGroup.",
               "results": [{"title": "T", "url": "https://d.dev", "content": "c"}]}
    monkeypatch.setattr("forge.tools.web._client", lambda: _httpx(_Response(payload=payload)))

    result = _run(WebSearch(), WebSearchArgs(query="x"), _ctx())

    assert "unverified" in result.content
    assert "Use TaskGroup." in result.content


# ── web_fetch ───────────────────────────────────────────────────────────────


def test_relative_url_is_refused_before_any_request(monkeypatch):
    """Tavily occasionally returns a redirect path; fetching it is meaningless."""
    monkeypatch.setattr("forge.tools.web._client",
                        lambda: _httpx(raises=AssertionError("must not be called")))

    result = _run(WebFetch(), WebFetchArgs(url="/goto?url=CAES"), _ctx())

    assert result.is_error and "absolute" in result.content


def test_html_is_stripped_to_readable_text(monkeypatch):
    html = ("<html><head><style>.a{color:red}</style></head><body>"
            "<script>var x=1;</script><h1>Timeouts</h1>"
            "<p>Default is 5&nbsp;seconds.</p></body></html>")
    monkeypatch.setattr("forge.tools.web._client", lambda: _httpx(
        _Response(text=html, headers={"content-type": "text/html; charset=utf-8"})))

    result = _run(WebFetch(), WebFetchArgs(url="https://d.dev/t"), _ctx())

    assert not result.is_error
    assert "Timeouts" in result.content and "Default is 5 seconds." in result.content
    assert "color:red" not in result.content and "var x=1" not in result.content


def test_javascript_only_page_says_so(monkeypatch):
    monkeypatch.setattr("forge.tools.web._client", lambda: _httpx(
        _Response(text="<html><body><div id='root'></div></body></html>",
                  headers={"content-type": "text/html"})))

    result = _run(WebFetch(), WebFetchArgs(url="https://d.dev"), _ctx())

    assert result.is_error and "JavaScript" in result.content


def test_oversized_page_is_truncated_with_a_marker(monkeypatch):
    monkeypatch.setattr("forge.tools.web._client", lambda: _httpx(
        _Response(text="x" * 5000, headers={"content-type": "text/plain"})))

    result = _run(WebFetch(), 
        WebFetchArgs(url="https://d.dev", max_chars=500), _ctx())

    assert not result.is_error and "[truncated at 500 chars of 5000]" in result.content


def test_404_is_reported(monkeypatch):
    monkeypatch.setattr("forge.tools.web._client",
                        lambda: _httpx(_Response(status=404, text="nope")))

    result = _run(WebFetch(), WebFetchArgs(url="https://d.dev/gone"), _ctx())

    assert result.is_error and "404" in result.content


def test_readable_drops_markup_but_keeps_prose():
    out = _readable("<div><script>bad()</script><p>Hello &amp; welcome</p></div>")
    assert "Hello & welcome" in out and "bad()" not in out
