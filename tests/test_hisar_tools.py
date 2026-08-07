"""The vault tools, against the server's actual permission model.

Three tiers, and the tools mirror them rather than reinventing them:

    list, download          machines, anywhere
    deposit                 machines, /SPEDA + /Forge only
    delete, rename          owner only — no tool exists at all

The tests worth having are the ones about the boundary, because the boundary is
asymmetric and surprising: an agent may READ the owner's private documents and
may not WRITE beside them. A model that assumes symmetry either does not look or
tries and is refused, and both cost a call.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.tools import hisar
from forge.tools.hisar import HisarDeposit, HisarList, HisarRead
from forge.warden.toolsource import resolve_optional, without_hisar_tools


class _Response:
    def __init__(self, status=200, payload=None, content=b"", text="") -> None:
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = text or (str(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeHttpx:
    """Stands in for the module, not the client — `hisar._client()` returns the
    module and the tool constructs `AsyncClient` from it."""

    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[dict] = []

    def AsyncClient(self, **_kw):  # noqa: N802 — mirrors httpx's own name
        outer = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def request(self, method, url, **kwargs):
                outer.calls.append({"method": method, "url": url, **kwargs})
                if isinstance(outer.response, Exception):
                    raise outer.response
                return outer.response

        return _C()


class _Cell:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or {}

    async def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class _Ctx:
    def __init__(self, cell=None) -> None:
        self.cell = cell


def _wire(monkeypatch, response, token="tok") -> _FakeHttpx:
    fake = _FakeHttpx(response)
    monkeypatch.setattr(hisar, "_client", lambda: fake)
    if token:
        monkeypatch.setenv("HISAR_MACHINE_TOKEN", token)
    else:
        monkeypatch.delenv("HISAR_MACHINE_TOKEN", raising=False)
    return fake


def _run(tool_cls, args):
    tool = tool_cls()
    return asyncio.run(tool.call(tool.Args.model_validate(args), _Ctx()))


# ── Withheld when there is no key ────────────────────────────────────────────

def test_unconfigured_is_not_a_tool_you_are_offered(monkeypatch):
    """Same rule as the graph tools: a door with no key is not shown. The model
    has no way to know a call will 401, and finds out by spending a turn."""
    monkeypatch.delenv("HISAR_MACHINE_TOKEN", raising=False)
    assert hisar.configured() is False
    tools = {"read_file": object(), "hisar_list": object(), "hisar_deposit": object()}
    assert set(resolve_optional(tools)) == {"read_file"}


def test_configured_keeps_them(monkeypatch):
    monkeypatch.setenv("HISAR_MACHINE_TOKEN", "tok")
    tools = {"read_file": object(), "hisar_list": object()}
    assert set(resolve_optional(tools)) == {"read_file", "hisar_list"}


def test_without_hisar_tools_removes_all_three():
    tools = {n: object() for n in
             ("hisar_list", "hisar_read", "hisar_deposit", "grep")}
    assert set(without_hisar_tools(tools)) == {"grep"}


# ── Reading is unrestricted ──────────────────────────────────────────────────

def test_list_reads_anywhere(monkeypatch):
    """Not just /Forge. The vault is the owner's filesystem and an agent that
    cannot see Documents cannot work from a document the owner named."""
    fake = _wire(monkeypatch, _Response(payload={"entries": [
        {"name": "taxes.pdf", "is_dir": False, "size": 91234},
        {"name": "Notes", "is_dir": True},
    ]}))
    out = _run(HisarList, {"path": "/Documents"})

    assert not out.is_error
    assert "taxes.pdf" in out.content and "Notes" in out.content
    assert fake.calls[0]["params"] == {"path": "/Documents"}


def test_an_empty_folder_says_so_rather_than_returning_nothing(monkeypatch):
    _wire(monkeypatch, _Response(payload={"entries": []}))
    out = _run(HisarList, {"path": "/Forge"})
    assert not out.is_error and "empty" in out.content


def test_read_returns_the_text(monkeypatch):
    _wire(monkeypatch, _Response(content="# Report\nbody\n".encode()))
    out = _run(HisarRead, {"path": "/Forge/report.md"})
    assert not out.is_error and "# Report" in out.content


def test_an_oversized_file_is_truncated_and_says_it_is(monkeypatch):
    """Silence about truncation is the dangerous version: the model concludes
    something is absent when it was merely cut off."""
    _wire(monkeypatch, _Response(content=("x" * 50_000).encode()))
    out = _run(HisarRead, {"path": "/Forge/big.log"})

    assert not out.is_error
    assert len(out.content) < 50_000
    assert "truncated" in out.content
    assert "do not conclude anything from its absence" in out.content


def test_a_binary_file_is_refused_legibly(monkeypatch):
    _wire(monkeypatch, _Response(content=b"\x89PNG\r\n\x1a\n\x00\xff\xfe"))
    out = _run(HisarRead, {"path": "/Forge/chart.png"})
    assert out.is_error and "binary" in out.content


# ── Writing is scoped, and refused locally ───────────────────────────────────

def test_deposit_outside_the_write_scope_is_refused_without_a_round_trip(monkeypatch):
    """The server would answer 403 identically. Refusing here costs no call AND
    can name the correct folders, which the 403 body does only by luck."""
    fake = _wire(monkeypatch, _Response(payload={"path": "/x"}))
    tool = HisarDeposit()
    out = asyncio.run(tool.call(
        tool.Args.model_validate({"path": "out.md", "folder": "/Documents"}),
        _Ctx(_Cell({"out.md": "hi"}))))

    assert out.is_error
    assert "/SPEDA" in out.content and "/Forge" in out.content
    assert fake.calls == [], "it should not have reached the network"


@pytest.mark.parametrize("folder", ["/Forge", "/SPEDA", "/Forge/reports"])
def test_deposit_inside_the_scope_goes_through(monkeypatch, folder):
    fake = _wire(monkeypatch, _Response(payload={"path": f"{folder}/out.md"}))
    tool = HisarDeposit()
    out = asyncio.run(tool.call(
        tool.Args.model_validate({"path": "out.md", "folder": folder}),
        _Ctx(_Cell({"out.md": "hi"}))))

    assert not out.is_error, out.content
    assert folder in out.content
    assert fake.calls[0]["method"] == "POST"


def test_deposit_reports_a_missing_source_rather_than_raising(monkeypatch):
    _wire(monkeypatch, _Response(payload={}))
    tool = HisarDeposit()
    out = asyncio.run(tool.call(
        tool.Args.model_validate({"path": "nope.md"}), _Ctx(_Cell())))
    assert out.is_error and "Could not read" in out.content


def test_there_is_no_delete_or_rename_tool():
    """The server answers 403 to a machine on both, always. A tool that can only
    fail costs a call to discover and teaches the model the vault is flaky."""
    from forge.tools import ALL_TOOLS

    assert not [n for n in ALL_TOOLS if "delete" in n or "rename" in n]


# ── Failures are results, never exceptions ───────────────────────────────────

def test_a_403_explains_the_permission_model(monkeypatch):
    """"Forbidden" alone reads as a malfunction and gets retried. Naming the
    tier tells the model to stop."""
    _wire(monkeypatch, _Response(status=403, text="Machine credentials may only write under /SPEDA, /Forge"))
    out = _run(HisarList, {"path": "/"})

    assert out.is_error
    assert "owner-only" in out.content
    assert "Do not retry" in out.content


def test_a_401_names_the_env_var_and_stops(monkeypatch):
    _wire(monkeypatch, _Response(status=401, text="Invalid machine token"))
    out = _run(HisarList, {"path": "/"})
    assert out.is_error
    assert "HISAR_MACHINE_TOKEN" in out.content and "do not retry" in out.content.lower()


def test_an_unreachable_vault_is_a_result(monkeypatch):
    _wire(monkeypatch, ConnectionError("no route to host"))
    out = _run(HisarList, {"path": "/"})
    assert out.is_error and "Could not reach Hisar" in out.content


def test_a_missing_token_is_a_result_not_a_crash(monkeypatch):
    _wire(monkeypatch, _Response(payload={}), token="")
    out = _run(HisarList, {"path": "/"})
    assert out.is_error and "not configured" in out.content


def test_the_token_is_sent_as_the_machine_header(monkeypatch):
    fake = _wire(monkeypatch, _Response(payload={"entries": []}), token="s3cret")
    _run(HisarList, {"path": "/"})
    assert fake.calls[0]["headers"]["X-Hisar-Token"] == "s3cret"


# ── The descriptions carry the boundary ──────────────────────────────────────

def test_the_write_scope_is_stated_rather_than_discovered():
    """An agent that has to be refused to learn the boundary spends a call on it.

    Only the tools that CAN be refused need to carry it. `hisar_read` cannot —
    reads are unrestricted, there is no boundary to hit — so naming the write
    scope there would be noise in a description that has to earn every word.
    The first version of this test asserted all three and caught nothing but
    its own over-reach."""
    for tool in (HisarList, HisarDeposit):
        text = tool.description
        assert "/SPEDA" in text and "/Forge" in text, tool.name


def test_the_read_asymmetry_is_stated():
    assert "NOT restricted" in HisarList.description
    assert "anywhere" in HisarRead.description
