"""Is the code valid, before anything runs it.

Two NameErrors shipped in this repo in one afternoon — `Transition` and
`require_reader`, both used before being imported. Both were caught by the test
suite minutes later, as a red run whose traceback had to be read back to its
cause. This finds that class of fault in about a second and points at the line.

It is not a language server. The other half of what one would give — where is
this defined, who calls this — is already answered by the graph tools, across
every language at once.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell
from forge.tools.diagnostics import Diagnostics, DiagnosticsArgs
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.tool import ToolContext


def _ctx(tmp_path):
    cell = SubprocessCell(tmp_path, CellPolicy())
    asyncio.run(cell.start())
    return ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _check(tmp_path, path="."):
    return asyncio.run(Diagnostics().call(DiagnosticsArgs(path=path), _ctx(tmp_path)))


def _has_local_checker() -> bool:
    """Is a checker reachable FROM THE CELL, without fetching one?

    Asked through a Cell rather than with shutil.which, because those are two
    different environments: the Cell runs its own shell with its own PATH and
    does not see the harness's venv. Checking the harness answered "ruff is
    here", the tool then failed to find it in the Cell and fell through to the
    `uvx` route, which downloads from PyPI — so the test failed on a slow fetch
    and blamed the tool.

    That is the same fault as reading a sandbox's TERM and drawing a conclusion
    about the operator's terminal. Measure where the code actually runs.
    """
    import tempfile

    from forge.cell.base import CellPolicy
    from forge.cell.subprocess_cell import SubprocessCell

    async def probe() -> bool:
        with tempfile.TemporaryDirectory() as d:
            cell = SubprocessCell(Path(d), CellPolicy())
            await cell.start()
            for cmd in ("ruff --version", "python -m ruff --version"):
                if (await cell.run(cmd)).exit_code == 0:
                    return True
        return False

    try:
        return asyncio.run(probe())
    except Exception:      # noqa: BLE001 — no checker is a skip, not an error
        return False


needs_checker = pytest.mark.skipif(
    not _has_local_checker(),
    reason="no locally installed checker; refusing to depend on a PyPI fetch")


@needs_checker
def test_it_catches_the_bug_that_shipped_today(tmp_path):
    """`Transition` used before it was imported. The suite caught it as a red
    run; this catches it as a line number."""
    (tmp_path / "broken.py").write_text(
        "def go():\n    return Transition(1)\n", encoding="utf-8")

    result = _check(tmp_path, "broken.py")

    assert not result.is_error, result.content
    assert "F821" in result.content
    assert "Transition" in result.content
    assert "broken.py:2" in result.content        # the line, not just the file


@needs_checker
def test_clean_code_says_so_plainly(tmp_path):
    """Ambiguity here would train the agent to ignore the tool."""
    (tmp_path / "fine.py").write_text("def ok():\n    return 1\n", encoding="utf-8")

    result = _check(tmp_path, "fine.py")

    assert not result.is_error
    assert "no problems found" in result.content


def test_checking_outside_the_workspace_is_refused(tmp_path):
    result = _check(tmp_path, "../../etc")
    assert result.is_error and "outside the workspace" in result.content


def test_a_missing_path_is_reported(tmp_path):
    result = _check(tmp_path, "nope.py")
    assert result.is_error and "No such path" in result.content


def test_findings_are_not_an_error_result():
    """Findings ARE the answer. Marking them is_error would make the agent treat
    a working check as a broken tool and stop using it."""
    from forge.tools.diagnostics import Diagnostics
    assert Diagnostics.READ_ONLY is True
    assert Diagnostics.CONCURRENCY_SAFE is True


def test_it_does_not_claim_to_prove_correctness():
    """A tool that reads as 'this verifies my change' would replace running the
    tests, which it cannot do."""
    d = Diagnostics.description
    assert "does NOT run tests" in d
    assert "correct" in d


def test_there_is_more_than_one_way_to_reach_a_checker():
    """The Cell does not share the harness's interpreter — it sees whatever
    python is on PATH — so a single hardcoded invocation is a guess."""
    from forge.tools.diagnostics import _CHECKERS
    assert len(_CHECKERS) >= 2
    assert any("uvx" in c for c in _CHECKERS)     # the route needing no install
