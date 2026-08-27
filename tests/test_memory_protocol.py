"""The memory *discipline* — the obey-and-feed layer the peer was missing.

The peer already receives the owner's memory block and has the tools to act on
it, but its system prompt never told it to OBEY the standing record or to FEED
it — so a rule the owner stated in one session ("keep the workspace clean and
structured") was obeyed once and gone by the next. Mark VI's in-process agents
get this from prompts/core/08_memory + 11_patterns; the peer runs its own prompt
and got neither. These tests hold the port: it says both halves, it names only
tools the peer actually has, and it is actually composed into the prompt on both
the connected and the offline path.
"""
from __future__ import annotations

from forge.agents.memory_protocol import memory_protocol_fragment


# ── Both halves are present ───────────────────────────────────────────────────

def test_the_online_form_tells_the_agent_to_obey_the_standing_record():
    text = memory_protocol_fragment(has_channel=True).text

    assert "dossier" in text
    assert "patterns.md" in text
    assert "prohibitions" in text
    # The pattern-loop half: design around it before committing to a plan.
    assert "plan" in text.lower()


def test_the_online_form_tells_the_agent_to_write_a_stated_rule_down():
    """The half that fixes the complaint: a standing rule is written to memory in
    the same turn, not obeyed once and dropped."""
    text = memory_protocol_fragment(has_channel=True).text
    # Line-wrapping in the prompt can split a phrase across a newline; compare on
    # whitespace-normalized text so an assertion is about words, not layout.
    flat = " ".join(text.split())

    assert "same turn" in flat
    assert "memory" in flat                       # the tool it writes with
    # The concrete failure the owner named, used as the worked example.
    assert "keep the workspace clean" in flat
    # And where a durable plan goes, since "they forget their plans" is the
    # other half of the report.
    assert "projects/" in flat
    assert "todo list does not survive" in flat


def test_the_offline_form_falls_back_to_the_one_capture_that_needs_no_channel():
    text = memory_protocol_fragment(has_channel=False).text

    # Offline the memory tool is withheld; remember_about_owner still works.
    assert "remember_about_owner" in text
    assert "withheld" in text
    # Obeying the snapshot still holds even when writing the shared store cannot.
    assert "OBEY" in text or "obey" in text


# ── Adapted to the peer's toolset, not Mark VI's ─────────────────────────────

def test_it_names_no_verb_the_peer_does_not_have():
    """The peer holds the FLAT memory tool, not Mark VI's section-aware verbs.
    Naming ledger_append or registry_upsert would tell it to call something that
    does not exist on its side — the exact failure the memory tool itself was
    added to fix, one layer along."""
    absent = ("ledger_append", "registry_upsert", "narrative_revise",
              "search_memory", "dispatch_agent", "search_history")
    for form in (memory_protocol_fragment(has_channel=True).text,
                 memory_protocol_fragment(has_channel=False).text):
        for verb in absent:
            assert verb not in form, verb


def test_the_online_form_uses_the_flat_commands_the_tool_actually_exposes():
    text = memory_protocol_fragment(has_channel=True).text

    # The flat memory tool's verbs — what the peer can genuinely call.
    assert "str_replace" in text
    assert "view" in text
    assert "create" in text


# ── It is a shared fragment, and it is actually wired in ─────────────────────

def test_it_is_labelled_shared_so_it_sorts_with_cross_cutting_discipline():
    from forge.agents.prompt import ORDER

    assert memory_protocol_fragment(has_channel=True).kind == "shared"
    assert "shared" in ORDER


def test_the_discipline_is_read_before_the_owner_block_it_refers_to():
    """The block Mark VI injects has an unknown kind and sorts last; a 'shared'
    fragment sorts ahead of it, so 'the Memory block' the discipline points at
    always follows the instruction to act on it."""
    from forge.agents.prompt import PromptFragment, compose_system_prompt

    composed = compose_system_prompt([
        PromptFragment("profile", "I am Optimus."),
        PromptFragment("owner memory (live from Mark VI)", "BLOCK BODY: dossier…"),
        memory_protocol_fragment(has_channel=True),
    ])
    assert composed.index("STANDING RULES") < composed.index("BLOCK BODY")


def test_the_connected_runner_composes_it_gated_on_the_channel():
    import inspect

    from forge.gate import runner

    src = inspect.getsource(runner.run_job)
    assert "memory_protocol_fragment(has_channel=ctx.memory is not None)" in src


def test_the_offline_tui_composes_the_offline_form():
    import inspect

    from forge.tui import repl

    src = inspect.getsource(repl._system_prompt)
    assert "memory_protocol_fragment(has_channel=False)" in src
