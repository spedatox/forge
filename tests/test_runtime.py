from __future__ import annotations

import asyncio

from forge.model.scripted import ScriptedModel
from forge.runtime import ExecutionSpec, execute


def test_identity_free_runtime_completes_without_persona_services(tmp_path):
    events = []

    async def emit(event):
        events.append(event)

    result = asyncio.run(execute(
        ExecutionSpec(
            job_id="job-1", role="reviewer", task="Review this empty workspace",
            workspace=tmp_path, model_ref="scripted",
        ),
        model=ScriptedModel([lambda _messages: ("No findings.", [])]),
        emit=emit,
    ))

    assert result.status == "succeeded"
    assert result.report == "No findings."
    assert events[0].type == "started"
    assert events[-1].type == "done"


def test_runtime_rejects_missing_workspace(tmp_path):
    async def emit(_event):
        return None

    try:
        asyncio.run(execute(
            ExecutionSpec(
                job_id="job-2", role="coder", task="x",
                workspace=tmp_path / "missing", model_ref="scripted",
            ),
            model=ScriptedModel([]), emit=emit,
        ))
    except ValueError as exc:
        assert "workspace" in str(exc)
    else:
        raise AssertionError("missing workspace was accepted")
