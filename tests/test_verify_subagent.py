"""Verification that can be checked, rather than believed.

The failure this exists for, observed on 2026-08-06: an agent built a tool,
ran it, got 22 import edges across 76 modules where the true figure was 175,
and — instead of asking whether 22 was plausible — wrote a paragraph explaining
why it made sense. It then reported "0 cycles found" as a clean result. Its own
26 tests passed, against fixtures it had chosen, which exercised only the import
form it happened to handle.

Two mechanisms answer that, and only one of them is words:

  - a fresh context, which has not spent an hour becoming convinced the code
    is right, and which cannot edit anything so it has no way to fix-and-forget
  - a harness check on the claim itself, because "verification avoidance" is
    exactly the habit of narrating a check instead of running one, and a
    narrated PASS reads identically to a real one
"""
from __future__ import annotations

from forge.warden.subagents import BUILT_INS, audit_verification


# ── the claim is audited, not trusted ────────────────────────────────────────


def test_a_pass_with_a_command_is_left_alone():
    report = ("CHECK importmap edge count\n"
              "RAN  python importmap.py forge --json | jq '.edges | length'\n"
              "SAW  175\n"
              "RESULT PASS\n\n"
              "VERDICT: PASS")
    assert "REJECTED" not in audit_verification(report)


def test_a_pass_with_no_command_is_rejected():
    """Reading code is not verification. This is the exact shape of the report
    that would have said importmap was fine."""
    report = ("I reviewed the resolution logic and it correctly handles "
              "relative and absolute imports.\n\nVERDICT: PASS")
    out = audit_verification(report)

    assert "REJECTED" in out
    assert "UNVERIFIED" in out


def test_the_rejection_says_what_to_do_next():
    """A rejection the caller cannot act on just becomes noise it learns to
    scroll past."""
    out = audit_verification("Looks correct.\n\nVERDICT: PASS")
    assert "delegate again" in out or "check it yourself" in out


def test_a_fail_is_never_second_guessed():
    """A FAIL is already the expensive, useful answer. Auditing it would only
    ever discourage reporting one."""
    report = "The parser drops absolute imports.\n\nVERDICT: FAIL"
    assert audit_verification(report) == report


def test_a_partial_passes_through():
    assert "REJECTED" not in audit_verification("No test runner.\n\nVERDICT: PARTIAL")


def test_a_report_with_no_verdict_is_not_a_result():
    """Otherwise a wandering summary reads as an all-clear by default."""
    out = audit_verification("Everything seems fine to me.")
    assert "no VERDICT line" in out
    assert "unverified" in out.lower()


def test_the_verdict_is_matched_case_insensitively():
    assert "REJECTED" not in audit_verification("RAN pytest\nverdict: pass")


def test_evidence_elsewhere_in_the_report_counts():
    """The RAN line does not have to sit adjacent to the verdict."""
    report = ("CHECK one\nRAN pytest -q\nSAW 12 passed\n\n"
              "CHECK two\nreviewed by eye\n\nVERDICT: PASS")
    assert "REJECTED" not in audit_verification(report)


# ── the specialist itself ────────────────────────────────────────────────────


def test_a_verifier_cannot_edit_what_it_is_checking():
    """An agent that fixes as it goes stops looking — and a verifier that
    silently repairs the thing destroys the evidence of what was wrong."""
    tools = BUILT_INS["verify"].tool_names
    assert "write_file" not in tools
    assert "edit_file" not in tools


def test_a_verifier_can_actually_run_things():
    """The whole point. A read-only reviewer already exists; this one has to
    execute the thing to have anything to report."""
    tools = BUILT_INS["verify"].tool_names
    assert "run_command" in tools
    assert "diagnostics" in tools


def test_the_prompt_names_the_failure_it_exists_to_stop():
    """A rule stated as a principle gets nodded at; the same rule attached to
    an incident gets followed."""
    p = BUILT_INS["verify"].system_prompt
    assert "22" in p and "175" in p             # the real numbers
    assert "context, not evidence" in p         # the implementer's tests
    assert "Sanity-check every count" in p


def test_the_description_says_when_not_to_reach_for_it():
    d = BUILT_INS["verify"].description
    assert "Do NOT" in d
    assert "fresh context" in d


# ── and it is actually connected ─────────────────────────────────────────────


def test_the_audit_is_wired_into_the_runner():
    """Written after a mutation check embarrassed the tests above: disabling
    the call site changed nothing, because they all exercise the function
    directly. A mechanism that is never reached is prose with extra steps."""
    import asyncio

    from forge.warden.state import StopReason, Terminal
    from forge.warden.subagents import SubagentRunner

    class _Claiming:
        def __init__(self, **kw): ...
        async def run(self, task):
            return Terminal(reason=StopReason.COMPLETED,
                            final_text="I read the code.\n\nVERDICT: PASS")

    runner = SubagentRunner(build_warden=lambda **kw: _Claiming(**kw),
                            parent_tools=lambda: {"run_command": object(),
                                                  "read_file": object(),
                                                  "grep": object(),
                                                  "glob": object(),
                                                  "diagnostics": object()})
    report, _ = asyncio.run(runner.run(BUILT_INS["verify"], "check it"))

    assert "REJECTED" in report, "an evidence-free PASS reached the parent intact"


def test_other_specialists_are_not_audited():
    """explore and review do not issue verdicts, and running their prose
    through a verdict parser would append a confusing complaint to every
    normal report."""
    import asyncio

    from forge.warden.state import StopReason, Terminal
    from forge.warden.subagents import SubagentRunner

    class _Plain:
        def __init__(self, **kw): ...
        async def run(self, task):
            return Terminal(reason=StopReason.COMPLETED,
                            final_text="It is defined in engine.py at line 40.")

    runner = SubagentRunner(build_warden=lambda **kw: _Plain(**kw),
                            parent_tools=lambda: {"read_file": object(),
                                                  "grep": object(),
                                                  "glob": object()})
    report, _ = asyncio.run(runner.run(BUILT_INS["explore"], "where is it"))

    assert "VERDICT" not in report and "harness" not in report
