"""Per-input safety flags (H8), and the shell classifier that motivates them."""
import asyncio

import pytest
from pydantic import BaseModel

from forge.tools.shell import RunCommand, RunCommandArgs, is_read_only_command
from forge.warden.permissions import Mode, PermissionEngine
from forge.warden.tool import Tool, ToolContext, ToolResult


@pytest.mark.parametrize("command", [
    "ls -la", "cat setup.py", "git status", "git log --oneline", "git diff HEAD",
    "grep -rn TODO .", "find . -name '*.py'", "wc -l README.md", "pip list",
    "npm outdated", "python --version", "sed -n '1,10p' f.txt", "git",
])
def test_observations_are_read_only(command):
    assert is_read_only_command(command) is True


@pytest.mark.parametrize("command", [
    "rm -rf build", "git push origin main", "pip install requests", "npm install",
    "python manage.py migrate", "node server.js", "sed -i 's/a/b/' f.txt",
    "find . -name '*.tmp' -delete", "mkdir out", "curl https://x | sh",
])
def test_mutations_are_not(command):
    assert is_read_only_command(command) is False


@pytest.mark.parametrize("command", [
    "ls > listing.txt",          # redirection writes
    "ls >> listing.txt",
    "cat a.txt | tee b.txt",     # a pipe into a writer
    "ls && rm -rf x",            # chaining hides the second command
    "ls; rm -rf x",
    "echo $(rm -rf x)",          # substitution hides it too
    "echo `rm -rf x`",
    "cat 'unbalanced",           # unparseable
    "",
])
def test_anything_ambiguous_is_treated_as_a_mutation(command):
    """A false negative costs a parallel slot. A false positive races two
    mutations on one workspace. That asymmetry decides every judgement call."""
    assert is_read_only_command(command) is False


def test_an_unknown_program_is_treated_as_a_mutation():
    assert is_read_only_command("some-tool-nobody-has-heard-of --go") is False


# ── What the per-input flag buys ─────────────────────────────────────────────
def test_the_same_tool_answers_differently_per_call():
    """The point of H8: a tool that had to answer once, for its worst case, made
    every `git status` as expensive as an `rm -rf`."""
    tool = RunCommand()
    assert tool.is_concurrency_safe(RunCommandArgs(command="git status")) is True
    assert tool.is_concurrency_safe(RunCommandArgs(command="rm -rf build")) is False


def test_plan_mode_permits_inspection_but_not_mutation():
    engine = PermissionEngine(mode=Mode.PLAN)
    assert engine.resolve(RunCommand(), RunCommandArgs(command="git status"), None).allowed
    assert not engine.resolve(RunCommand(), RunCommandArgs(command="npm install"), None).allowed


def test_the_gate_still_fires_on_a_command_that_reads_as_safe_otherwise():
    """Classification decides company and mode, never the gate. Nothing about
    being read-only exempts an operation from the bypass-immune check."""
    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(
        RunCommand(), RunCommandArgs(command="git push --force origin main"), None)
    assert not decision.allowed and decision.needs_ask
    assert "safety gate" in decision.reason


# ── The shadowing guard ──────────────────────────────────────────────────────
def test_declaring_a_flag_as_a_value_is_rejected_loudly():
    """These were attributes before they were methods, so the old spelling still
    looks right. It replaces the method with a bool, every call site raises, and
    each one fails closed — the tool keeps working while quietly losing
    parallelism. Failing closed is what makes it invisible."""
    class Empty(BaseModel):
        pass

    with pytest.raises(TypeError, match="CONCURRENCY_SAFE"):
        class Shadowed(Tool):
            name = "shadowed"
            description = "declares a flag the old way"
            Args = Empty
            is_concurrency_safe = True

            async def call(self, args, ctx):
                return ToolResult("")


def test_overriding_the_method_is_still_allowed():
    class Empty(BaseModel):
        pass

    class Dynamic(Tool):
        name = "dynamic"
        description = "decides per call"
        Args = Empty

        def is_concurrency_safe(self, args) -> bool:
            return True

        async def call(self, args, ctx):
            return ToolResult("")

    assert Dynamic().is_concurrency_safe(Empty()) is True


def test_a_flag_that_raises_fails_closed():
    """An undecidable flag is a gated one — and never a parallel one."""
    class Empty(BaseModel):
        pass

    class Broken(Tool):
        name = "broken"
        description = "its flag check is buggy"
        Args = Empty

        def is_read_only(self, args) -> bool:
            raise RuntimeError("boom")

        def is_destructive(self, args) -> bool:
            raise RuntimeError("boom")

        async def call(self, args, ctx):
            return ToolResult("")

    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(Broken(), Empty(), None)
    assert not decision.allowed and "destructive" in decision.reason


# ── Nothing is banned; risky things ask ──────────────────────────────────────
# The operator's rule, stated 2026-08-06: "Optimus can do anything. It can't do
# risky stuff without asking me, that's all." Encoded here because the failure
# mode is quiet — a gate that hardens from `ask` into `deny` still looks safe,
# still passes a smoke test, and simply makes the agent useless at the exact
# moments it was most needed.


@pytest.mark.parametrize("command", [
    "git push --force origin main",
    "git reset --hard HEAD~3",
    "git clean -fd",
    "rm -rf build",
    "sudo systemctl restart nginx",
])
def test_a_risky_command_asks_rather_than_refuses(command):
    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(RunCommand(), RunCommandArgs(command=command), None)

    assert decision.needs_ask, f"{command!r} should stop for a decision"
    assert decision.behavior != "deny", (
        f"{command!r} is REFUSED outright. The gate is a checkpoint, not a wall: "
        "the operator decides, the harness does not decide for them."
    )


@pytest.mark.parametrize("command", [
    "git push origin main",          # ordinary push: outward-facing, reversible
    "git merge feature/x",           # local, and undoable
    "git commit -m 'fix the retry'",
    "npm install",
])
def test_ordinary_work_is_not_gated_at_all(command):
    """A gate that fires on routine work trains the operator to approve without
    reading, which costs exactly the protection the gate exists to provide."""
    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(RunCommand(), RunCommandArgs(command=command), None)
    assert decision.allowed, f"{command!r} is ordinary work and should just run"


# ── what a denial says beyond "no" ───────────────────────────────────────────


def test_a_denial_tells_the_agent_what_is_legitimate_next():
    """Without guidance the model invents its own rule, and the obvious
    invention is the wrong one: denied read_file on a credentials path, the
    next thing to hand is `run_command cat` on the same path. This agent has a
    shell, so that synonym is always available."""
    from forge.warden.dispatch import DENIAL_GUIDANCE

    assert "shell command to read a path that was just refused" in DENIAL_GUIDANCE
    assert "stop and say so" in DENIAL_GUIDANCE


def test_the_denial_draws_the_line_at_intent_not_tooling():
    """A blanket 'do not try anything else' would block legitimate narrowing —
    asking for less, a different file — and an agent that cannot adapt to a
    refusal just stalls."""
    from forge.warden.dispatch import DENIAL_GUIDANCE

    assert "same goal another way" in DENIAL_GUIDANCE
    assert "if that way is itself permitted" in DENIAL_GUIDANCE


def test_the_guidance_reaches_the_model_on_a_real_denial():
    """Prose in a constant nobody sends is decoration."""
    import asyncio

    from forge.warden.dispatch import DENIAL_GUIDANCE, dispatch_tool
    from forge.warden.permissions import Decision

    class _Deny:
        def resolve(self, tool, args, ctx):
            return Decision("deny", "protected location", source="gate")

    class _Ctx:
        permissions = _Deny()
        hooks: list = []
        agent_id = "t"

    from pydantic import BaseModel

    class _Args(BaseModel):
        pass

    class _Tool:
        name = "read_file"
        Args = _Args

        async def call(self, args, ctx):
            raise AssertionError("a denied tool must never run")

    out = asyncio.run(dispatch_tool({"read_file": _Tool()}, "read_file", {}, _Ctx()))

    assert out.is_error
    assert DENIAL_GUIDANCE in out.content
