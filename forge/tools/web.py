"""Research: `web_search` and `web_fetch`.

The gap these close is debugging. Without them an agent reasons about a stack
trace purely from what is in the repo and what it happened to memorise during
training — it cannot read the library's docs, look up the error text, or check
whether an API signature changed in the version actually installed. That is the
difference between fixing a bug and guessing at one.

**Warden-side, not in the Cell — and deliberately not gated on
`ctx.network_allowed`.** `search.py` already draws this line: the Cell's
isolation posture governs code the model *writes and runs*, not the harness's
own instruments. Conflating the two would mean "let the agent read documentation"
also means "let whatever it just compiled phone home", which is backwards. A
profile can keep `allow_network = false` on its Cell — nothing the model executes
reaches the network — while the agent itself can still read the web. The two
are separate capabilities and the security posture is stronger for saying so.

**Degrades rather than breaks.** No key configured, no HTTP client installed, a
provider outage, a 404 — every one of them comes back as an ordinary is_error
result the model can read and route around, the same contract the graph tool
uses when Graphify is absent. A research tool that raises kills the turn; one
that explains itself costs an iteration.
"""
from __future__ import annotations

import json
import os
import re

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_SEARCH_ENDPOINT = "https://api.tavily.com/search"
_HTTP_TIMEOUT_S = 25.0
# A fetched page is raw material, not the answer. The cap keeps one oversized
# doc page from evicting the actual work from the context window; the model can
# always fetch again with a narrower target.
_DEFAULT_FETCH_CHARS = 12_000
_MAX_FETCH_CHARS = 40_000

_KEY_ENV = "TAVILY_API_KEY"

_NO_KEY = (
    f"Web search is not configured: {_KEY_ENV} is not set in this Forge's "
    "environment. This is an operator setup task, not something you can fix — "
    "say so and continue with the repository itself. Do not retry."
)
_NO_CLIENT = (
    "Web access is unavailable: the `httpx` package is not installed in this "
    "Forge. Operator setup — do not retry; continue without it."
)

# Tags whose *contents* are markup, not prose. Dropped whole, before tag
# stripping, or the reader gets a page's worth of CSS and JS.
_NOISE = re.compile(
    r"<(script|style|noscript|svg|head|template)\b[^>]*>.*?</\1>",
    re.I | re.S,
)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def _client():
    """The HTTP client, or None when the dependency is absent."""
    try:
        import httpx
    except ImportError:
        return None
    return httpx


def _readable(html: str) -> str:
    """Rough text extraction — good enough to read documentation.

    Deliberately dependency-free regex rather than a real parser. A doc page
    read for its prose does not need correct DOM semantics, and adding
    beautifulsoup/lxml to a harness that must install cleanly on an operator's
    Windows box is a poor trade for marginally tidier whitespace.
    """
    text = _NOISE.sub(" ", html)
    text = _TAG.sub(" ", text)
    # Entities worth the two lines; anything rarer survives as-is and reads fine.
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(line for line in lines if line))


class WebSearchArgs(BaseModel):
    query: str = Field(description="What to search for. Write it as you would type it into a search engine.")
    max_results: int = Field(default=5, ge=1, le=10, description="How many results to return (1-10, default 5).")


class WebSearch(Tool):
    name = "web_search"
    description = (
        "Search the web and get back ranked results with a title, URL, and an "
        "extracted snippet for each. Use it when the answer is not in this "
        "repository: an unfamiliar error message, a library's current API, a "
        "changelog, whether a known bug exists upstream. Do NOT use it for "
        "anything the repo can answer — grep and read_file are faster and "
        "authoritative for your own code. The snippets are often enough on their "
        "own; call web_fetch on a result URL only when you need the full page."
    )
    Args = WebSearchArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True   # several independent lookups are the normal case

    async def call(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        httpx = _client()
        if httpx is None:
            return ToolResult(_NO_CLIENT, is_error=True)
        key = os.environ.get(_KEY_ENV, "").strip()
        if not key:
            return ToolResult(_NO_KEY, is_error=True)

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
                response = await client.post(
                    _SEARCH_ENDPOINT,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json={"query": args.query, "max_results": args.max_results,
                          "include_answer": True},
                )
        except Exception as e:  # noqa: BLE001 — every failure is a readable result
            return ToolResult(f"Web search failed to reach the provider: {e}", is_error=True)

        if response.status_code != 200:
            detail = response.text[:200]
            return ToolResult(
                f"Web search returned HTTP {response.status_code}: {detail}",
                is_error=True,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return ToolResult("Web search returned a malformed response.", is_error=True)

        results = payload.get("results") or []
        if not results:
            return ToolResult(
                f"No results for {args.query!r}. Try different wording, or a more "
                "specific error string.",
            )

        parts: list[str] = []
        answer = (payload.get("answer") or "").strip()
        if answer:
            # A synthesised answer is a lead, not a citation — label it so the
            # model quotes the sources rather than this.
            parts.append(f"Summary (unverified, check the sources below):\n{answer}\n")
        for i, item in enumerate(results, 1):
            title = str(item.get("title") or "(untitled)").strip()
            url = str(item.get("url") or "").strip()
            content = " ".join(str(item.get("content") or "").split())
            parts.append(f"{i}. {title}\n   {url}\n   {content}")
        return ToolResult("\n\n".join(parts))


class WebFetchArgs(BaseModel):
    url: str = Field(description="Absolute URL to fetch, including the scheme (https://...).")
    max_chars: int = Field(
        default=_DEFAULT_FETCH_CHARS, ge=500, le=_MAX_FETCH_CHARS,
        description=f"Cap on returned text (default {_DEFAULT_FETCH_CHARS}).")


class WebFetch(Tool):
    name = "web_fetch"
    description = (
        "Fetch one URL and return its readable text with markup stripped. Use it "
        "to read a documentation page, an issue thread, or a changelog you found "
        "via web_search or that the operator gave you. Do NOT use it to browse "
        "speculatively, to fetch a page you already have the answer from in a "
        "search snippet, or to reach anything on a private network. Returns the "
        "page text truncated to max_chars, or an is_error explaining what went "
        "wrong (404, timeout, non-HTML content)."
    )
    Args = WebFetchArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: WebFetchArgs, ctx: ToolContext) -> ToolResult:
        httpx = _client()
        if httpx is None:
            return ToolResult(_NO_CLIENT, is_error=True)

        url = args.url.strip()
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult(
                f"{url!r} is not an absolute http(s) URL. Search results give "
                "absolute URLs — pass one of those verbatim.",
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_S, follow_redirects=True,
            ) as client:
                response = await client.get(
                    url, headers={"User-Agent": "Forge/1.0 (+coding-agent)"},
                )
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"Could not fetch {url}: {e}", is_error=True)

        if response.status_code != 200:
            return ToolResult(
                f"{url} returned HTTP {response.status_code}.", is_error=True,
            )

        content_type = response.headers.get("content-type", "")
        body = response.text
        if "html" in content_type.lower():
            body = _readable(body)
        elif not any(t in content_type.lower()
                     for t in ("text", "json", "xml", "javascript", "")):
            return ToolResult(
                f"{url} is {content_type!r}, not text. Nothing to read.",
                is_error=True,
            )

        body = body.strip()
        if not body:
            return ToolResult(
                f"{url} fetched successfully but produced no readable text — it is "
                "probably rendered by JavaScript. Try a different source.",
                is_error=True,
            )
        if len(body) > args.max_chars:
            body = body[: args.max_chars] + (
                f"\n\n[truncated at {args.max_chars} chars of {len(body)}]"
            )
        return ToolResult(f"{url}\n\n{body}")
