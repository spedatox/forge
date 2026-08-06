"""The interactive loop — Forge's local surface.

`demo`, `serve` and `connect` all run a job somebody else asked for. This is the
one entry point where a person is present for the whole thing, which makes it
the only place several Mark II mechanisms can actually be observed: the
permission ask resolves against a human instead of a timeout, compaction happens
while you watch, and the ledger is answering a question you can ask mid-session.

**One Cell for the session, not one per turn.** `run_job` builds a fresh Cell per
job because a dispatched job is a closed unit. A conversation is not: the file
you wrote in turn three has to still be there in turn nine, and `cd` has to
survive. So the session owns the Cell and each turn borrows it.

**Ctrl-C interrupts the turn, not the session.** The engine already checks its
abort signal at both boundaries and returns a clean ABORTED terminal, so the
handler here only has to set the signal and let the loop unwind — the transcript
stays well-formed and the next prompt starts from a consistent state.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from forge.agents.registry import AgentRegistry
from forge.cell.factory import build_cell
from forge.cell.base import CellPolicy
from forge.config import ForgeSettings
from forge.extensions import load_extensions
from forge.gate.protocol import JobRequest
from forge.tui import ansi
from forge.tui.commands import command_help, resolve as resolve_command
from forge.tui.render import StreamRenderer, banner, humanize_error
from forge.tui.input import InputBar
from forge.tui.session import Session
from forge.tui.spinner import Spinner
from forge.tui import persistence, status, ui
from forge.warden.compaction import (
    elide_old_tool_results,
    find_cut,
    rebuild,
    render_for_summary,
    summarize,
)
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.ledger import TokenLedger
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason
from forge.warden.subagents import SubagentRunner
from forge.warden.tool import ToolContext
from forge.warden.toolsource import close_providers, fold_providers, without_graph_tools


async def run_repl(agent: str = "optimus", workspace: Path | None = None,
                   verbose: bool = False, model_override: str | None = None) -> int:
    settings = ForgeSettings.from_env()
    registry = AgentRegistry.load()
    try:
        cfg = registry.get(agent)
    except KeyError as e:
        ansi.write(ansi.paint(f"  {e}", "red"))
        return 1

    workspace = Path(workspace or Path.cwd()).resolve()
    model_ref = model_override or cfg.model_ref
    extensions = load_extensions()
    providers = extensions.tool_providers()

    ansi.enable()
    ui.reset()   # the console must be built AFTER the colour decision
    cell = None
    try:
        cell = await build_cell(
            agent_id=cfg.agent_id, workspace_root=settings.workspace_root,
            backend=cfg.cell.backend or settings.cell_backend,
            image=cfg.cell.image or settings.cell_image,
            policy=CellPolicy(allow_network=cfg.cell.allow_network,
                              cpus=cfg.cell.cpus, memory_mb=cfg.cell.memory_mb,
                              default_timeout_s=cfg.cell.timeout_s,
                              run_as_root=cfg.cell.run_as_root,
                              cap_add=cfg.cell.cap_add,
                              # So a commit records who actually wrote it.
                              env=cfg.git.env()),
            workspace=workspace)

        request = JobRequest(agent=cfg.agent_id, task="", repo_path=str(workspace))
        # The session holds the FULL set; the per-turn filter in _run_turn
        # decides what the model sees. The REPL starts no sidecar, so the graph
        # QUERY tools are withheld until graph_index builds one — they would
        # otherwise answer "unavailable" to a model their own descriptions told
        # to try them first.
        tools = await fold_providers(providers, cfg, request)

        session = Session(
            cfg=cfg, model_ref=model_ref, workspace=workspace, tools=tools, cell=cell,
            ledger=TokenLedger(context_limit=settings.context_limit,
                               max_output_tokens=settings.max_tokens),
            allowlist=AllowList.load(settings.allowlist_path),
            session_id=persistence.new_id())

        ansi.write(banner(f"{cfg.name} ({cfg.agent_id})", model_ref, str(workspace), len(tools)))
        return await _loop(session, settings, extensions, verbose)
    except Exception as e:  # noqa: BLE001 — a failed start should say why, not traceback
        ansi.write(ansi.paint(f"  could not start: {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await close_providers(providers)
        if cell is not None:
            await cell.close()


async def _loop(session: Session, settings: ForgeSettings, extensions, verbose: bool) -> int:
    bar = InputBar(session.workspace, command_help(),
                   on_cycle_mode=lambda: _cycle_permission_mode(session),
                   on_toggle_expand=lambda: _expand_last(session),
                   hint=lambda: _hint_line(session))
    session.input_bar = bar
    if bar.degraded_reason:
        # Say it once, at the top, where it explains the session you are about
        # to have. Without this the prompt looks completely normal and simply
        # does less: no completion menu on `/` or `@`, and a multi-line paste
        # submits at its first newline instead of arriving whole.
        ansi.write(ansi.paint(
            "  ! no line editor here — completions and multi-line paste are off",
            "yellow"))
        ansi.write(ansi.paint(f"    {bar.degraded_reason}", "dim"))
        ansi.write(ansi.paint(
            "    put long input in a file and ask me to read it", "dim"))
    while True:
        # Rule and counters CLOSE the exchange above them, then a blank line,
        # then the prompt. Sitting directly on top of the prompt they read as
        # a header for what is about to be typed, which is the wrong tense:
        # `106 in / 39 out` describes the turn that just finished.
        ansi.write()
        ansi.write(ansi.paint("─" * max(10, ansi.terminal_width() - 1), "dim"))
        status.write(session)
        ansi.write()
        entry = await bar.read(ansi.paint("› ", "cyan"))
        if entry.is_eof:
            ansi.write()
            return 0
        if not entry.text:
            continue
        _echo_prompt(entry.text)

        if entry.kind == "command":
            outcome = await _run_command(entry.text, session)
            if outcome is True:
                return 0
            if isinstance(outcome, str) and outcome:
                # A command asked for a turn (/review). Run it as if typed.
                await _run_turn(outcome, session, settings, extensions, verbose)
        elif entry.kind == "bash":
            await _run_bash(entry.text, session)
        else:
            await _run_turn(entry.text, session, settings, extensions, verbose)


def _echo_prompt(text: str) -> None:
    """Repaint the line just typed as a full-width band.

    A question and its answer are otherwise two paragraphs of identical
    text and the eye has to read them to tell which is which. A filled row
    is recognised before it is read, which is what makes a long transcript
    skimmable.

    The line editor has already echoed what was typed, so this reclaims
    those rows and prints the band over them — the same rewind the reply
    uses. When it cannot (styling off, or the input scrolled), the echo
    stands and only the blank line separates the two.
    """
    band = ui.user_message(text)
    if band:
        rows = ansi.wrapped_height("› " + " ".join(text.split()))
        if ansi.rewind(rows):
            ansi.write(band)
    ansi.write()


def _hint_line(session: Session) -> str:
    """What sits under the input.

    Only what changes with state or is otherwise undiscoverable. A static
    row of every shortcut becomes furniture within a day; the mode belongs
    here because it silently governs whether the next request can write
    anything at all."""
    bits = ["? /help", "!cmd shell", "@file path"]
    mode = session.permission_mode
    bits.append("shift+tab plan" if mode == "act" else "PLAN — edits denied")
    return ansi.paint("  " + "   ".join(bits), "dim")


async def _run_bash(command: str, session: Session) -> None:
    """`!cmd` — run it in the Cell and print the result. No model turn.

    The escape hatch for everything that does not need reasoning: `git status`,
    `ls`, `pytest -x`. Spending a full model turn to have the agent decide to
    run `git status` costs a round trip and tokens to reach a foregone
    conclusion, and the operator already knew what they wanted to run.

    It does NOT enter the transcript. The model did not ask for this and did not
    see it, so presenting it as part of the conversation would be a lie about
    what the agent knows — if its result matters, say so in the next prompt.
    """
    if session.cell is None:
        ansi.write(ansi.paint("  no Cell attached — nothing to run in", "yellow"))
        return
    ansi.write(ansi.paint(f"  ! {command}", "grey"))
    try:
        result = await session.cell.run(command)
    except Exception as e:  # noqa: BLE001 — a bad command must not end the session
        ansi.write(ansi.paint(f"  failed: {e}", "red"))
        return
    for stream, style in ((result.stdout, None), (result.stderr, "red")):
        text = (stream or "").rstrip()
        if not text:
            continue
        for line in text.splitlines():
            ansi.write(ansi.paint(f"  {line}", style) if style else f"  {line}")
    if result.exit_code != 0:
        ansi.write(ansi.paint(f"  exit {result.exit_code}", "yellow"))


def _expand_last(session: Session) -> None:
    """ctrl+o — put back what the one-line view cut.

    An inline renderer cannot reach up and rewrite output that has already
    scrolled, so "expand" means printing the last shortened result again, in
    full. That is also when the operator actually wants it: they read the
    truncated line, and only then decide they needed the rest."""
    if not session.last_truncated:
        ansi.write(ansi.paint("\n  nothing to expand", "dim"))
        return
    name, text = session.last_truncated
    ansi.write()
    ansi.write(ansi.paint(f"  ⏶ {name} — full output", "cyan"))
    for line in text.splitlines() or [""]:
        ansi.write("      " + ansi.paint(line, "grey"))


def _cycle_permission_mode(session: Session) -> None:
    """shift+tab — act / plan, the way Claude Code cycles its modes.

    Plan mode denies every mutation outright, so this is the one-key way to say
    "look, don't touch" before asking something exploratory."""
    order = ["act", "plan"]
    try:
        nxt = order[(order.index(session.permission_mode) + 1) % len(order)]
    except ValueError:
        nxt = "act"
    session.set_permission_mode(nxt)
    ansi.write(ansi.paint(f"\n  permission mode: {nxt}", "cyan"))


async def _run_command(line: str, session: Session) -> "bool | str":
    """True to end the session, a string to run as a turn, else False."""
    cmd, args = resolve_command(line)
    if cmd is None:
        ansi.write(ansi.paint(f"  unknown command: {line.split()[0]} — try /help", "yellow"))
        return False

    result = await cmd.run(args, session)
    if result.text:
        ansi.write(result.text)
    if result.clear:
        session.reset()
        ansi.write(ansi.paint("  conversation cleared", "grey"))
    if result.compact:
        await _compact_now(session)
    if result.quit:
        return True
    return result.prompt or False


async def _compact_now(session: Session) -> None:
    """`/compact` on demand — the same two layers the engine runs itself."""
    before = len(session.messages)
    messages, freed = elide_old_tool_results(session.messages, keep_cycles=1)
    session.messages = messages
    if freed:
        ansi.write(ansi.paint(f"  ◆ reclaimed {freed:,} chars of old tool output", "magenta"))

    cut = find_cut(session.messages, keep_cycles=2)
    if cut is None:
        ansi.write(ansi.paint("  nothing further to summarize", "grey"))
        return

    ansi.write(ansi.paint("  ◆ summarizing…", "magenta"))
    model = _build_model(session)
    summary = await summarize(model, render_for_summary(session.messages[1:cut]),
                              asyncio.Event())
    if summary is None:
        ansi.write(ansi.paint("  the summary call failed; nothing was changed", "yellow"))
        return
    session.messages = rebuild(session.messages, cut, summary)
    # The model's memory of file contents is the summary's now, not the cache's.
    ansi.write(ansi.paint(
        f"  ◆ {before} messages → {len(session.messages)}", "magenta"))


def _build_model(session: Session):
    from forge.model.factory import build_model
    settings = ForgeSettings.from_env()
    return build_model(session.model_ref, settings, max_tokens=settings.max_tokens)


async def _run_turn(prompt: str, session: Session, settings: ForgeSettings,
                    extensions, verbose: bool) -> None:
    signal = asyncio.Event()
    spinner = Spinner()
    # The oracle has to be able to stop the live line: a permission prompt is
    # printed and then repainted over several times a second, which erases the
    # question and everything typed into it. A new Spinner exists per turn, so
    # the handoff happens here rather than at Session construction.
    session.oracle.spinner = spinner
    renderer = StreamRenderer(
        verbose=verbose, spinner=spinner,
        on_truncated=lambda name, text: setattr(session, "last_truncated", (name, text)),
    )
    files = FileStateCache()

    ctx = ToolContext(
        agent_id=session.cfg.agent_id, cell=session.cell, graph=None, files=files,
        permissions=PermissionEngine(mode=Mode(session.permission_mode),
                                     allowlist=session.allowlist),
        network_allowed=session.cfg.cell.allow_network,
        oracle=session.oracle,
        hooks=list(extensions.hooks),
    )

    model = _build_model(session)

    async def _tools() -> dict:
        """The session's tools, minus the graph queries until a graph exists.

        The REPL starts no sidecar, so those tools would answer "unavailable"
        to a model their own descriptions told to try them first. `graph_index`
        is always present and can build one mid-session — this re-check is what
        lets the query tools appear afterwards instead of staying withheld for
        a graph the agent has just created."""
        live = ctx.graph
        if live is not None and getattr(live, "available", False):
            return dict(session.tools)
        return without_graph_tools(session.tools)

    warden = Warden(
        system_prompt=_system_prompt(session, extensions),
        tools=await _tools(), model=model, ctx=ctx,
        max_iterations=session.cfg.max_iterations, signal=signal,
        ledger=session.ledger,
        retry_attempts=settings.retry_attempts,
        retry_base_delay=settings.retry_base_delay_s,
        refresh_tools=_tools,
        emit=renderer,
    )

    # Subagents get the same Cell, model, interrupt signal and ledger as the
    # turn that spawned them — they differ only in prompt, toolset, and having
    # their own message list. Sharing `signal` is what makes ctrl+c reach a
    # child; sharing `ledger` is what keeps /cost honest.
    ctx.subagents = SubagentRunner(
        build_warden=lambda **kw: Warden(
            model=model, ctx=ctx, signal=signal, ledger=session.ledger,
            retry_attempts=settings.retry_attempts,
            retry_base_delay=settings.retry_base_delay_s,
            **kw,              # includes the child's scoped emit
        ),
        parent_tools=lambda: session.tools,
        emit=renderer,
    )

    # Continue the conversation rather than starting one: seed the loop with
    # everything said so far, so turn nine remembers turn three.
    warden_task = asyncio.create_task(_drive(warden, session, prompt))
    spinner.start()
    try:
        terminal = await asyncio.shield(warden_task)
    except KeyboardInterrupt:
        # Ask the loop to stop at its next boundary. It back-fills any pending
        # tool_results so the transcript stays well-formed, then returns ABORTED.
        signal.set()
        spinner.set_status("Interrupting")
        ansi.write()
        ansi.write(ansi.paint("  ⏹ interrupting…", "yellow"))
        terminal = await warden_task
    except asyncio.CancelledError:
        signal.set()
        raise
    finally:
        # Always: an exception must not leave a spinner redrawing over the
        # next prompt forever.
        await spinner.stop()

    session.messages = list(terminal.messages)
    session.turns += 1
    persistence.save(session, session.session_id)
    _report(terminal, session, verbose, already_shown=renderer.saw_error)


async def _drive(warden: Warden, session: Session, prompt: str):
    """Run one turn over the session's accumulated transcript."""
    if session.messages:
        warden_state_messages = [*session.messages, {"role": "user", "content": prompt}]
        return await warden.run_messages(warden_state_messages)
    return await warden.run(prompt)


def _system_prompt(session: Session, extensions) -> str:
    """The standalone TUI's prompt.

    This path never has Mark VI on the other end, so the owner's memory comes
    from the snapshot the peer path cached — labelled as a snapshot, and dated,
    so stale facts are not stated as current. See forge/agents/owner_memory.py.
    """
    from forge.agents import conventions, owner_memory
    from forge.agents.prompt import PromptFragment, compose_system_prompt

    repo = conventions.fragment(session.workspace)
    owner = owner_memory.offline_fragment()
    return compose_system_prompt([
        PromptFragment("profile", session.cfg.system_prompt),
        *([owner] if owner else []),
        *([repo] if repo else []),
        *extensions.fragments,
    ])


def _report(terminal, session: Session, verbose: bool, already_shown: bool = False) -> None:
    if terminal.reason is StopReason.ABORTED:
        ansi.write(ansi.paint("  ⏹ stopped", "yellow"))
    elif terminal.reason is StopReason.MAX_ITERATIONS:
        # The transcript is intact and the next prompt continues from it, so say
        # that. Reporting only the ceiling reads as "the work is lost", and an
        # operator who does not know they can simply say "carry on" starts the
        # whole job again.
        ansi.write(ansi.paint(
            f"  ⏹ hit the {session.cfg.max_iterations}-iteration ceiling — "
            "the work so far is kept", "yellow"))
        ansi.write(ansi.paint(
            "    say 'continue' to carry on from here, or /compact first if the "
            "context is full", "dim"))
    elif terminal.reason is StopReason.ERROR and not already_shown:
        # The renderer usually showed this already, via the error event. Saying
        # it twice makes one failure look like two.
        ansi.write(ansi.paint(f"  ✗ {humanize_error(terminal.error or '')}", "red"))

    if verbose or terminal.reason is not StopReason.COMPLETED:
        usage = terminal.usage or {}
        ansi.write(ansi.paint(
            f"  {terminal.iterations} iterations · {usage.get('prompt', 0):,} tokens in context",
            "dim"))
