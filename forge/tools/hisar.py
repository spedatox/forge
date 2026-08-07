"""H.İ.S.A.R. — the owner's file vault, as three tools.

Forge could not touch the vault at all. The only artifact anywhere was
`hisar/integrations/speda_hisar_skill.py`, a reference implementation targeting
speda-mark6 that says in its own header it does not run where it lives — and
whose description still calls the vault "write-only for agents", which stopped
being true when the server widened machine reads. A contract file that has
drifted from the contract is the thing it was written to prevent.

**The server's permission model is not ours to redesign.** It is three tiers and
these tools mirror it exactly:

    list, download          machines, anywhere            require_reader
    deposit, upload, mkdir  machines, /SPEDA + /Forge      authorize_write
    delete, rename          owner only, never a machine    require_owner

`hisar/server/auth.py` argues the read/write asymmetry better than a summary
could: "an agent that can only WRITE is close to useless: it cannot find the
report it filed last week, check whether a folder already exists, or read a
document the owner asked it to work from."

**No delete and no rename tool, deliberately.** The server answers 403 to a
machine on both, every time. A tool that can only fail is worse than an absent
one — it costs a call to discover and teaches the model that Hisar is flaky.
Forge already holds this line: graph tools are withheld when there is no graph
rather than offered and failing.

**Warden-side, not in the Cell.** `web.py` draws the same line: the Cell's
network posture governs code the model writes and runs; the harness's own
instruments are separate. The vault holds the owner's documents, and it must not
be reachable from generated code that happens to be running with network on.

Withheld entirely when the token is unset, so an agent is never offered a door
it has no key to.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

_TOKEN_ENV = "HISAR_MACHINE_TOKEN"
_URL_ENV = "HISAR_URL"
_DEFAULT_URL = "https://hisar.spedatox.systems"
_HTTP_TIMEOUT_S = 30.0

# Mirrors hisar/server/config.py MACHINE_WRITE_SCOPES. Duplicated rather than
# imported because Forge does not depend on the Hisar package — and named here
# so the tool descriptions can state the boundary instead of letting the model
# discover it by being refused.
WRITE_SCOPES = ("/SPEDA", "/Forge")

# A vault document is raw material, not the answer. Same cap and same reasoning
# as web_fetch: one oversized file must not evict the actual work from context.
_MAX_READ_CHARS = 20_000

_NO_TOKEN = (
    f"Hisar is not configured: {_TOKEN_ENV} is not set in this Forge's "
    "environment. This is an operator setup task, not something you can fix — "
    "say so and continue without the vault. Do not retry."
)
_NO_CLIENT = (
    "Hisar is unavailable: the `httpx` package is not installed in this Forge. "
    "Operator setup — do not retry; continue without it."
)


def configured() -> bool:
    """Whether this Forge has a vault to talk to. Read by the tool source, which
    withholds all three tools when it is false."""
    return bool(os.environ.get(_TOKEN_ENV, "").strip())


def _base_url() -> str:
    return os.environ.get(_URL_ENV, "").strip().rstrip("/") or _DEFAULT_URL


def _client():
    try:
        import httpx
    except ImportError:
        return None
    return httpx


def _headers() -> dict[str, str]:
    return {"X-Hisar-Token": os.environ.get(_TOKEN_ENV, "").strip()}


def _explain(status: int, body: str) -> str:
    """Turn a vault HTTP failure into something with a next step in it.

    403 is the one worth spelling out. It is not a malfunction — it is the
    permission model working, and a model that reads "Forbidden" concludes the
    tool is broken and retries. Naming the tier it hit tells it to stop.
    """
    detail = body[:200].strip()
    if status == 401:
        return (f"Hisar rejected the machine token ({_TOKEN_ENV}). Operator "
                f"setup — do not retry. {detail}")
    if status == 403:
        return (f"Hisar refused: {detail or 'forbidden'}. Machines may write "
                f"only under {', '.join(WRITE_SCOPES)}, and may never delete or "
                f"rename — those are owner-only by design. Reading is not "
                f"restricted; a different path or a read will work where this "
                f"did not. Do not retry this call unchanged.")
    if status == 404:
        return f"Hisar has no such path: {detail or 'not found'}."
    return f"Hisar returned HTTP {status}: {detail}"


async def _request(method: str, path: str, **kwargs) -> tuple[object | None, str]:
    """One vault call. Returns (response, "") or (None, readable error).

    Every failure comes back as a value. A tool that raises kills the turn; one
    that explains itself costs an iteration — the same contract every other
    networked tool in this harness keeps.
    """
    httpx = _client()
    if httpx is None:
        return None, _NO_CLIENT
    if not configured():
        return None, _NO_TOKEN
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            response = await client.request(
                method, f"{_base_url()}{path}", headers=_headers(), **kwargs)
    except Exception as e:  # noqa: BLE001 — unreachable vault is a result, not a crash
        return None, f"Could not reach Hisar at {_base_url()}: {e}"
    if response.status_code >= 400:
        return None, _explain(response.status_code, response.text)
    return response, ""


# ── list ─────────────────────────────────────────────────────────────────────

class HisarListArgs(BaseModel):
    path: str = Field(default="/", description="Vault folder to list, e.g. '/' or '/Forge'.")


class HisarList(Tool):
    name = "hisar_list"
    description = (
        "List a folder in H.İ.S.A.R., the owner's file vault. Use it to find "
        "something you or the owner filed earlier, to check whether a folder "
        "already exists before depositing into it, or to see what the owner has "
        "before asking them. Reading is NOT restricted by folder — you can list "
        "anywhere in the vault, including the owner's own documents. Writing is "
        f"restricted to {' and '.join(WRITE_SCOPES)}; deleting and renaming are "
        "owner-only and you have no tool for them."
    )
    Args = HisarListArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: HisarListArgs, ctx: ToolContext) -> ToolResult:
        response, error = await _request("GET", "/files/list",
                                         params={"path": args.path})
        if response is None:
            return ToolResult(error, is_error=True)
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return ToolResult("Hisar returned a malformed listing.", is_error=True)

        entries = payload.get("entries") or payload.get("items") or []
        if not entries:
            return ToolResult(f"{args.path} is empty.")
        lines = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            name = e.get("name") or e.get("path") or "?"
            kind = "dir " if (e.get("is_dir") or e.get("type") == "dir") else "file"
            size = e.get("size")
            lines.append(f"  {kind}  {name}" + (f"  ({size:,} bytes)"
                                                if isinstance(size, int) else ""))
        return ToolResult(f"{args.path}\n" + "\n".join(lines))


# ── read ─────────────────────────────────────────────────────────────────────

class HisarReadArgs(BaseModel):
    path: str = Field(description="Full vault path of the file, e.g. '/Forge/report.md'.")


class HisarRead(Tool):
    name = "hisar_read"
    description = (
        "Read a text file out of H.İ.S.A.R., the owner's file vault. Use it when "
        "the owner refers to something already in the vault, or to pick up work "
        "you filed in an earlier session. You can read anywhere in the vault. "
        "Large files are truncated — if you need the rest, say so rather than "
        "guessing at it. Binary files (images, PDFs, archives) cannot be read "
        "as text; list the folder to confirm what a file is before reading it."
    )
    Args = HisarReadArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: HisarReadArgs, ctx: ToolContext) -> ToolResult:
        response, error = await _request("GET", "/files/download",
                                         params={"path": args.path})
        if response is None:
            return ToolResult(error, is_error=True)

        raw = response.content
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                f"{args.path} is not UTF-8 text ({len(raw):,} bytes) — it looks "
                "like a binary file. This tool reads text only.", is_error=True)

        if len(text) > _MAX_READ_CHARS:
            kept = text[:_MAX_READ_CHARS]
            return ToolResult(
                f"{kept}\n\n[… truncated at {_MAX_READ_CHARS:,} of "
                f"{len(text):,} characters. This is the head of the file, not "
                f"all of it — do not conclude anything from its absence.]")
        return ToolResult(text)


# ── deposit ──────────────────────────────────────────────────────────────────

class HisarDepositArgs(BaseModel):
    path: str = Field(description="Path IN THE CELL WORKSPACE of the file to deposit.")
    folder: str = Field(default="/Forge",
                        description=f"Vault folder to deposit into. Must be under "
                                    f"{' or '.join(WRITE_SCOPES)}.")
    filename: str | None = Field(default=None,
                                 description="Name in the vault. Defaults to the source filename.")


class HisarDeposit(Tool):
    name = "hisar_deposit"
    description = (
        "Put a file into H.İ.S.A.R., the owner's permanent file vault. Use it "
        "for a deliverable worth keeping — a report, an export, a build "
        "artifact the owner asked for. The Cell workspace is scratch and does "
        "not survive; the vault does. Do NOT use it for intermediate working "
        "files, or as a substitute for simply showing the owner a short result. "
        f"You may write only under {' and '.join(WRITE_SCOPES)} — anywhere else "
        "is refused. Deposits never overwrite: a name collision is resolved by "
        "the server. Depositing is user-visible work, so say where it went in "
        "one sentence."
    )
    Args = HisarDepositArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False

    async def call(self, args: HisarDepositArgs, ctx: ToolContext) -> ToolResult:
        if not any(args.folder == s or args.folder.startswith(s + "/")
                   for s in WRITE_SCOPES):
            # Refused here rather than by the server. The answer is identical
            # and this one costs no round trip — and it can name the correct
            # folders, which a 403 body does only by luck.
            return ToolResult(
                f"{args.folder!r} is outside the folders a machine may write to. "
                f"Deposit under {' or '.join(WRITE_SCOPES)} instead.", is_error=True)

        cell = getattr(ctx, "cell", None)
        if cell is None:
            return ToolResult("No Cell workspace to read the file from.", is_error=True)
        try:
            content = await cell.read(args.path)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"Could not read {args.path} from the workspace: {e}",
                              is_error=True)

        name = args.filename or args.path.replace("\\", "/").rsplit("/", 1)[-1]
        response, error = await _request(
            "POST", "/deposit",
            data={"folder": args.folder, "filename": name},
            files={"file": (name, content.encode("utf-8", "replace"))},
        )
        if response is None:
            return ToolResult(error, is_error=True)
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {}
        landed = payload.get("path") or f"{args.folder}/{name}"
        return ToolResult(f"Deposited to {landed} in the vault.")
