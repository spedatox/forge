"""What is broken right now, without running the whole suite.

This is the half of a language server that a coding agent actually needs. The
other half — where is this defined, who calls this, what is coupled to what —
is already answered by the graph tools, and answered across languages at once,
which no single language server does.

What the graph cannot tell you is whether the code you just wrote is valid. Two
NameErrors shipped in this repo in a single afternoon (`Transition` and
`require_reader`, both used before being imported) and both were caught by the
test suite, minutes later, as a red run whose traceback had to be read back to
its cause. A linter finds that class of fault in under a second, before anything
is executed, and points straight at the line.

Deliberately NOT a language server. Spawning one per language and speaking
JSON-RPC over document-sync buys go-to-definition and rename on top of this, and
costs a process lifecycle, an editor state machine, and a per-language install.
Running the project's own checker gets the diagnostics — the part that changes
whether the agent notices it broke something — for a subprocess call.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

# Enough to see the shape of the problem; past this the agent should fix some
# and look again, not read a wall.
MAX_FINDINGS = 40


class DiagnosticsArgs(BaseModel):
    path: str = Field(
        default=".",
        description="File or directory to check, relative to the workspace.")


class Diagnostics(Tool):
    name = "diagnostics"
    display_name = "Diagnostics"
    description = (
        "Reports syntax errors, undefined names, unused imports and similar faults "
        "in code you have just written — the things that make it fail before any "
        "test gets to run. Use it after an edit and BEFORE running the test suite: "
        "it takes about a second and points straight at the line, whereas a red "
        "suite makes you read a traceback back to its cause. Also use it on a file "
        "you are about to change, so you know which problems were already there. It "
        "does NOT run tests and does not prove your change is correct — it proves "
        "the code is valid, which is a different and smaller claim. Returns a list "
        "of file:line findings, or says the code is clean."
    )
    Args = DiagnosticsArgs

    READ_ONLY = True
    CONCURRENCY_SAFE = True
    DESTRUCTIVE = False

    async def _run_checker(self, cell, rel: str) -> tuple[str | None, str]:
        """(payload, stderr). payload is None when no checker could be reached.

        Tries each route until one produces output that looks like a report.
        "command not found" is not a failure of the check — it is a failure to
        find the checker, and the next route may still work.
        """
        last_err = ""
        for template in _CHECKERS:
            result = await cell.run(template.format(path=_quote(rel)))
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            if out.startswith("[") or out.startswith("{"):
                return out, err
            # ruff prints nothing and exits 0 when a file is clean.
            if result.exit_code == 0 and not err:
                return "", err
            last_err = err or last_err
        return None, last_err

    async def call(self, args: DiagnosticsArgs, ctx: ToolContext) -> ToolResult:
        cell = getattr(ctx, "cell", None)
        if cell is None:
            return ToolResult("No workspace to check.", is_error=True)

        root = Path(cell.host_path).resolve()
        target = (root / (args.path or ".")).resolve()
        if target != root and root not in target.parents:
            return ToolResult(
                f"Refused: {args.path} is outside the workspace.", is_error=True)
        if not target.exists():
            return ToolResult(f"No such path: {args.path}", is_error=True)

        rel = args.path or "."
        raw, stderr = await self._run_checker(cell, rel)

        if raw is None:
            return ToolResult(
                "No checker is available here. Install one in the workspace "
                f"(`pip install ruff`) or run the tests instead.\n{stderr[:200]}",
                is_error=True)

        # ruff exits non-zero when it finds problems, which is not an error here
        # — findings ARE the answer. Only an unparseable payload is a failure.
        if not raw:
            return ToolResult(f"{rel}: no problems found.")

        try:
            findings = json.loads(raw)
        except json.JSONDecodeError:
            return ToolResult(f"Could not read the checker's output:\n{raw[:400]}",
                              is_error=True)

        if not findings:
            return ToolResult(f"{rel}: no problems found.")

        lines = []
        for f in findings[:MAX_FINDINGS]:
            where = f.get("filename", "?")
            try:
                where = str(Path(where).resolve().relative_to(root).as_posix())
            except (ValueError, OSError):
                pass
            loc = (f.get("location") or {})
            code = f.get("code") or ""
            lines.append(
                f"  {where}:{loc.get('row', '?')}:{loc.get('column', '?')} "
                f"{code} {f.get('message', '')}".rstrip())

        more = ""
        if len(findings) > MAX_FINDINGS:
            more = (f"\n  … and {len(findings) - MAX_FINDINGS} more. Fix these and "
                    "run it again rather than reading the rest.")
        return ToolResult(
            f"{len(findings)} problem(s) in {rel}:\n" + "\n".join(lines) + more)


def _quote(path: str) -> str:
    """A path with spaces is the common case on this operator's machine."""
    return "'" + path.replace("'", "'\\''") + "'"


#: How to reach a checker, best first. The Cell does not share the harness's
#: interpreter — it sees whatever python is on PATH — so "python -m ruff" is a
#: guess, not a given. `uvx` is last because it may fetch on first use, and
#: first because it is the only one that needs nothing installed anywhere: on a
#: machine with uv it turns "no checker here" into a working checker.
_CHECKERS = (
    "ruff check {path} --output-format json",
    "python -m ruff check {path} --output-format json",
    "uvx --quiet ruff check {path} --output-format json",
)

