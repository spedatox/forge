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

That was the intent and it did not hold. Relying on the default `KeyboardInterrupt`
meant the interrupt was raised wherever the interpreter happened to be — which,
during a turn, is almost always inside the Warden task rather than at the
`await` watching it. The task then finished *carrying* KeyboardInterrupt, the
handler's `await warden_task` re-raised it, and it unwound past this module to
`__main__`, where `except KeyboardInterrupt: return 0` ended the process. Ctrl-C
killed Forge instead of the turn, which is the opposite of what this paragraph
promised.

So the signal is now captured for the duration of a turn (`_interrupts_go_to`)
and converted into the abort event directly, and no exit path from `_run_turn`
is allowed to propagate an interrupt. A SECOND ctrl-C inside the same turn does
force its way out — an agent wedged somewhere the boundaries cannot see must
still be escapable, and a polite request that cannot be repeated is a trap.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal as signalmod
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from forge.agents.registry import AgentRegistry
from forge.cell.factory import build_cell
from forge.cell.base import CellPolicy
from forge.config import ForgeSettings
from forge.extensions import load_extensions
from forge.gate.protocol import JobRequest
from forge.tui import ansi, attach
from forge.tui.commands import command_help, resolve as resolve_command
from forge.tui.render import StreamRenderer, banner, humanize_error
from forge.tui import input as input_mod
from forge.tui.input import InputBar
from forge.tui.session import Session
from forge.tui.composer import Composer
from forge.tui.live import LiveRegion
from forge.tui import notify, persistence, status, ui
from forge.warden.compaction import (
    elide_old_tool_results,
    find_cut,
    rebuild,
    render_for_summary,
    summarize,
)
from forge.warden import images
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.inbox import Inbox
from forge.warden.ledger import TokenLedger
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason, Terminal
from forge.warden.subagents import SubagentRunner
from forge.warden.tool import ToolContext
from forge.warden.toolsource import (close_providers, fold_providers,
                                     resolve_optional, without_graph_tools,
                                     without_memory_tools, without_recall_tools)


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
                              # A person is present for this whole session, so
                              # they are the stopping condition rather than the
                              # clock. The profile's timeout still governs when
                              # they are TOLD a command has run long; it no
                              # longer decides on their behalf. Safe only here,
                              # and only because the two things it depends on
                              # are also only here: live output above, and a
                              # ctrl+c that reaches the loop.
                              kill_on_timeout=False,
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
        tools = resolve_optional(await fold_providers(providers, cfg, request))

        session = Session(
            cfg=cfg, model_ref=model_ref, workspace=workspace, tools=tools, cell=cell,
            ledger=TokenLedger(context_limit=settings.context_limit,
                               max_output_tokens=settings.max_tokens),
            allowlist=AllowList.load(settings.allowlist_path),
            extensions=extensions,
            session_id=persistence.new_id())

        ansi.write(banner(f"{cfg.name} ({cfg.agent_id})", model_ref, str(workspace), len(tools)))
        return await _loop(session, settings, extensions, verbose)
    except Exception as e:  # noqa: BLE001 — a failed start should say why, not traceback
        ansi.write(ansi.paint(f"  could not start: {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await close_providers(providers)
        # Plugins go with the providers, and after them: a plugin may have
        # published a service a provider is still using while it closes.
        extensions.unload()
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
        status.write(session)
        # Claude Code frames its input; the rule Forge drew above the status
        # line was doing the same job — separating the exchange above from what
        # is about to be typed — with a horizontal line instead of a box. The
        # box does it better because it also marks where the input ENDS, which
        # a rule cannot: with a bare `›` a wrapped three-line paste and the
        # transcript above it are the same shape.
        ansi.write(ui.prompt_top() or ansi.paint(
            "─" * max(10, ansi.terminal_width() - 1), "dim"))
        # Text typed during the last turn that no boundary claimed. Pre-filled
        # rather than submitted: the turn it was aimed at is over, so the
        # operator gets to see it in context and decide whether it still says
        # what they meant before it becomes a prompt of its own.
        carried, session.pending_prompt = session.pending_prompt, ""
        entry = await bar.read(ui.prompt_lead() or ansi.paint("› ", "grey"),
                               prefill=carried)
        if entry.is_eof:
            ansi.write()
            return 0
        if not entry.text:
            continue
        entry = _fold_if_huge(entry, session)
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
            await _run_turn(_attach(entry.text, session), session, settings,
                            extensions, verbose)


def _attach(text: str, session: Session):
    """Turn a typed line into the content the turn carries.

    Returns the text unchanged when nothing in it named a picture, which is
    almost every line — so the transcript keeps its plain-string shape and only
    a turn that actually has an image pays the block-list form.

    Anything that looked like an image and could not be sent is reported here
    and the turn goes ahead without it. Losing the whole prompt to one unreadable
    path would cost the question that was typed with it.
    """
    found = attach.from_prompt(text, session.workspace)
    for note in found.notes:
        ansi.write(ansi.paint(f"  ⚠ {note}", "yellow"))
    if found.names:
        ansi.write(ansi.paint(
            f"  ◆ attached {', '.join(found.names)}", "magenta"))
    return found.content


def _fold_if_huge(entry, session: Session):
    """Fold an oversized paste before it becomes a turn.

    Only for prompts. A `!command` that long is the operator's business and
    folding it would corrupt the command; a `/command` that long is a typo.

    The operator is told, and in the same breath told where the rest went —
    a paste that silently loses its middle is worse than one that is refused,
    because the model answers confidently about text it never saw.
    """
    if entry.kind != "prompt" or len(entry.text) <= input_mod.PASTE_THRESHOLD:
        return entry
    spill = session.workspace / ".forge" / "pastes" / f"{session.session_id}-{session.turns}.txt"
    folded, withheld = input_mod.fold_paste(entry.text, spill_path=spill)
    if not withheld:
        return entry
    ansi.write(ansi.paint(
        f"  ⚠ that paste was {len(entry.text):,} characters — sending the first and "
        f"last {input_mod.PASTE_KEEP} with the middle elided", "yellow"))
    if spill.exists():
        ansi.write(ansi.paint(f"    full text: {spill}", "dim"))
    return replace(entry, text=folded)


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

    def _live(_stream: str, text: str) -> None:
        """Straight through, unindented and uncoloured.

        The operator typed this command themselves and is watching it happen;
        the right rendering of `npm run build` is the one `npm run build`
        produces. Indenting each line would break every progress bar and every
        carriage-return redraw the tool is doing on purpose."""
        ansi.write(text, end="")

    try:
        result = await session.cell.run(command, on_output=_live)
    except Exception as e:  # noqa: BLE001 — a bad command must not end the session
        ansi.write(ansi.paint(f"  failed: {e}", "red"))
        return
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
        status.forget_pressure(session)
        ansi.write(ansi.paint("  conversation cleared", "grey"))
    if result.compact:
        await _compact_now(session)
        status.forget_pressure(session)
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


def _build_model(session: Session, model_ref: str | None = None):
    from forge.model.factory import build_model
    settings = ForgeSettings.from_env()
    return build_model(model_ref or session.model_ref, settings,
                       max_tokens=settings.max_tokens)


def _model_ref_for(session: Session, pending: list[dict]) -> str:
    """Which model this turn runs on.

    Normally the session's. A turn carrying a picture goes to the profile's
    `vision_model` instead, because the default here is text-only and the image
    would otherwise reach a provider that cannot read it.

    `--model` still wins: an operator who named a model on the command line
    meant it, and overruling that silently would be the worse surprise. The
    picture is then sent to what they picked and refused out loud if it cannot
    see, which is the outcome §9.5 asks for.
    """
    cfg = session.cfg
    if not cfg.vision_model or session.model_ref != cfg.model_ref:
        return session.model_ref
    if not images.has_image([*session.messages, *pending]):
        return session.model_ref
    return cfg.vision_model


def _aborted_terminal(session: Session, partial: "list[dict] | None" = None) -> Terminal:
    """What a forced stop leaves behind.

    The transcript is whatever the session already had, unless the caller hands
    back the warden's own committed transcript — a forced cancel has no Terminal
    to read, but the loop held onto everything it had written before the cancel,
    so nothing about that turn's completed work is lost. The next turn re-seeds
    from here, and `repair_transcript` fixes any tool_use left without its result
    — which is exactly the shape a forced cancel produces."""
    messages = list(partial) if partial is not None else list(session.messages)
    return Terminal(reason=StopReason.ABORTED, final_text="",
                    iterations=0, error=None, messages=messages,
                    transitions=(), usage=session.ledger.snapshot())


async def _settle(task: "asyncio.Task", session: Session) -> Terminal:
    """Wait out an interrupted turn without letting anything escape.

    Every ending is a Terminal: the loop's own ABORTED if it reached a boundary,
    a synthesised one if it was cancelled or raised on the way out. The one
    outcome not permitted here is an exception, because the caller's job is to
    print a result and return to the prompt."""
    try:
        return await task
    except (asyncio.CancelledError, KeyboardInterrupt):
        return _aborted_terminal(session)
    except Exception:  # noqa: BLE001 — the turn is over either way
        return _aborted_terminal(session)


@contextlib.contextmanager
def _interrupts_go_to(abort: asyncio.Event, notify: "Callable[[int], None]"):
    """Route SIGINT into `abort` for the duration of a turn, then put it back.

    Installed per turn rather than per process, because ctrl-C means two
    different things in the two states. At the prompt it should clear the line
    or leave, which `InputBar` already handles through the normal
    KeyboardInterrupt; mid-turn it must reach the loop's abort event instead of
    being raised into whatever coroutine frame happened to be executing.

    `signal.signal` rather than `loop.add_signal_handler`: the latter is not
    implemented on Windows' Proactor loop, and this is the surface a person uses
    on a laptop. The handler runs in the main thread between bytecodes, so it
    hands the work to the loop with `call_soon_threadsafe` instead of touching
    the event directly.

    The second press escalates. The first is a request the loop honours at its
    next boundary — which is the right default, because it lets the transcript
    close cleanly — but a boundary that is never reached would make that request
    unanswerable, and an operator holding a terminal that ignores ctrl-C has no
    move left. So the count is kept, and the second press cancels for real.

    Restores the previous handler unconditionally: a turn that raised must not
    leave the next prompt with a hijacked ctrl-C.
    """
    loop = asyncio.get_running_loop()
    presses = 0

    def _on_sigint(_signum, _frame) -> None:
        nonlocal presses
        presses += 1
        count = presses
        loop.call_soon_threadsafe(abort.set)
        loop.call_soon_threadsafe(notify, count)

    try:
        previous = signalmod.signal(signalmod.SIGINT, _on_sigint)
    except (ValueError, OSError):
        # Not the main thread, or a platform that will not let us. The old
        # behaviour is still better than refusing to run the turn.
        yield lambda: 0
        return
    try:
        yield lambda: presses
    finally:
        try:
            signalmod.signal(signalmod.SIGINT, previous)
        except (ValueError, OSError):
            pass


async def _run_turn(prompt: Any, session: Session, settings: ForgeSettings,
                    extensions, verbose: bool) -> None:
    signal = asyncio.Event()
    spinner = LiveRegion()
    # The oracle has to be able to stop the live line: a permission prompt is
    # printed and then repainted over several times a second, which erases the
    # question and everything typed into it. A new Spinner exists per turn, so
    # the handoff happens here rather than at Session construction.
    session.oracle.spinner = spinner
    # And the interrupt, for the same reason. A prompt is the one thing that can
    # park a turn past every boundary where the signal would be checked, so it
    # has to be able to read the signal itself — otherwise ctrl+c on a gated
    # `run_command` sets a flag nothing ever looks at.
    session.oracle.signal = signal
    # Per turn, not per session: an inbox that outlived its turn would deliver
    # last turn's afterthought into this one's opening move, which is worse
    # than losing it. Anything unclaimed at the end is carried forward
    # explicitly and visibly instead (see the end of this function).
    inbox = Inbox()
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
        on_command_output=renderer.command_output,
        bus=extensions.bus,
    )

    turn_model_ref = _model_ref_for(session, [{"role": "user", "content": prompt}])
    if turn_model_ref != session.model_ref:
        # Never invisible. The status line says one model and this turn used
        # another; unannounced, the next /cost reads as a billing mystery.
        ansi.write(ansi.paint(f"  ◆ image in this turn — using {turn_model_ref}", "magenta"))
    model = _build_model(session, turn_model_ref)

    async def _tools() -> dict:
        """The session's tools, minus the graph queries until a graph exists.

        The REPL starts no sidecar, so those tools would answer "unavailable"
        to a model their own descriptions told to try them first. `graph_index`
        is always present and can build one mid-session — this re-check is what
        lets the query tools appear afterwards instead of staying withheld for
        a graph the agent has just created.

        The owner's memory goes for the same reason and permanently: it lives
        in Mark VI, this path never has Mark VI on the other end, and unlike a
        graph nothing here can bring one into existence mid-session. The
        offline snapshot in the system prompt already says so in words. Recall
        over past sessions and the agent channel reach Mark VI over the same
        absent socket, so they go with it."""
        available = without_recall_tools(without_memory_tools(session.tools))
        live = ctx.graph
        if live is not None and getattr(live, "available", False):
            return available
        return without_graph_tools(available)

    warden = Warden(
        system_prompt=_system_prompt(session, extensions),
        tools=await _tools(), model=model, ctx=ctx,
        max_iterations=session.cfg.max_iterations, signal=signal,
        ledger=session.ledger,
        retry_attempts=settings.retry_attempts,
        retry_base_delay=settings.retry_base_delay_s,
        refresh_tools=_tools,
        emit=renderer,
        inbox=inbox,
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
    started = time.monotonic()

    # The composer polls stdin for the length of the turn. It must be attached
    # BEFORE the region starts drawing, so the first frame already has room for
    # it rather than the region growing a row under the operator's hands.
    composer = Composer(inbox, on_abort=lambda: _pressed(_bump()))
    spinner.attach_composer(composer)
    spinner.start()
    composer.start()

    presses = 0

    def _bump() -> int:
        """Ctrl+c read as a KEY rather than delivered as a signal.

        In raw mode the tty does not raise SIGINT, so while the composer is
        polling this is the only path an interrupt takes. It shares the counter
        with the signal handler so the first press is always the polite one and
        the second always forces, regardless of which route each arrived by —
        an operator pressing twice must not be told twice that they can press
        again."""
        nonlocal presses
        presses += 1
        signal.set()
        return presses

    def _pressed(count: int) -> None:
        """Say what the press did. Silence here is what makes an operator press
        again harder — and the second press is the one that hurts."""
        spinner.set_status("Interrupting")
        ansi.write()
        if count == 1:
            ansi.write(ansi.paint(
                "  ⏹ interrupting — finishing the current step so the "
                "transcript stays intact (ctrl+c again to force)", "yellow"))
        else:
            ansi.write(ansi.paint("  ⏹ forcing the turn to stop…", "red"))
            warden_task.cancel()

    try:
        with _interrupts_go_to(signal, _pressed):
            try:
                terminal = await asyncio.shield(warden_task)
            except asyncio.CancelledError:
                # The forced path: the second press cancelled the task. That is
                # the operator's decision arriving, not a failure, so it becomes
                # an ABORTED terminal like any other stop. The warden can no longer
                # return its Terminal, but it held onto the transcript it had
                # committed before the cancel — recover it so the operator's prompt
                # and every tool round that already ran survive to the next turn.
                if not warden_task.cancelled():
                    raise
                terminal = _aborted_terminal(session, warden.recover_transcript())
            except KeyboardInterrupt:
                # Only reachable if the handler could not be installed. Same
                # ending: ask, wait, and never let it past this frame.
                signal.set()
                terminal = await _settle(warden_task, session)
    except KeyboardInterrupt:
        # The last line of defence. An interrupt raised anywhere in the block
        # above — including inside `_settle` — must not reach `_loop`, because
        # `__main__` reads it as "leave" and ends the session. The turn is over;
        # the conversation is not.
        signal.set()
        terminal = await _settle(warden_task, session)
    finally:
        # Always: an exception must not leave a spinner redrawing over the
        # next prompt forever. The composer stops first — it writes into the
        # region's frame, and a poller still running while the region tears
        # down would draw into rows that no longer exist.
        leftover = await composer.stop()
        await spinner.stop()

    session.messages = list(terminal.messages)
    session.turns += 1
    persistence.save(session, session.session_id)
    _report(terminal, session, verbose, already_shown=renderer.saw_error,
            seconds=time.monotonic() - started)

    # Nothing the operator typed is lost. Two ways it can survive the turn: a
    # half-finished draft, and a message queued so late that the loop reached
    # its Terminal before any boundary could claim it. Both come back as the
    # next prompt rather than disappearing — text that vanishes is how an input
    # line stops being trusted, and an operator who has been burned once starts
    # waiting for turns to end before typing, which is the whole feature gone.
    carried = [*inbox.peek(), *( [leftover] if leftover.strip() else [] )]
    if carried:
        inbox.claim()
        session.pending_prompt = "\n\n".join(carried)
        ansi.write(ansi.paint(
            f"  ┆ carried over to the next prompt: "
            f"{ansi.truncate(carried[0], 60)}"
            f"{f' (+{len(carried) - 1} more)' if len(carried) > 1 else ''}",
            "cyan"))


async def _drive(warden: Warden, session: Session, prompt: Any):
    """Run one turn over the session's accumulated transcript.

    `prompt` is a string for an ordinary line and a content-block list when the
    operator attached a picture. Both go through `run_messages`, which takes the
    message whole — `warden.run` exists to wrap a bare string and would have to
    grow a second shape to carry blocks."""
    return await warden.run_messages(
        [*session.messages, {"role": "user", "content": prompt}])


def _system_prompt(session: Session, extensions) -> str:
    """The standalone TUI's prompt.

    This path never has Mark VI on the other end, so the owner's memory comes
    from the snapshot the peer path cached — labelled as a snapshot, and dated,
    so stale facts are not stated as current. See forge/agents/owner_memory.py.
    """
    from forge.agents import conventions, owner_memory
    from forge.agents.memory_protocol import memory_protocol_fragment
    from forge.agents.prompt import PromptFragment, compose_system_prompt

    repo = conventions.fragment(session.workspace)
    owner = owner_memory.offline_fragment()
    location = _workspace_location_fragment(session)
    # The obey-and-feed discipline for the snapshot — offline form, so it points
    # at remember_about_owner (which needs no channel) rather than the withheld
    # memory tool. Only when there is actually a snapshot to obey; with no cached
    # block there is nothing for it to refer to.
    memory_protocol = memory_protocol_fragment(has_channel=False) if owner else None
    return compose_system_prompt([
        PromptFragment("profile", session.cfg.system_prompt),
        *([owner] if owner else []),
        *([memory_protocol] if memory_protocol else []),
        *([location] if location else []),
        *([repo] if repo else []),
        *extensions.fragments,
    ])


def _workspace_location_fragment(session: Session):
    """Tell the agent where it actually is, so it does not give generic
    sandbox disclaimers when it is working inside Hisar or a named project."""
    from forge.agents.prompt import PromptFragment
    from forge.tools.hisar import configured as hisar_configured

    has_hisar = hisar_configured() and "hisar" in session.cfg.tools
    path_str = str(session.workspace)

    if has_hisar and "hisar" in path_str.lower():
        text = (
            f"Your workspace is at {path_str}.\n\n"
            "This is a folder inside H.İ.S.A.R., the S.P.E.D.A. network's "
            "cloud file vault. Files you create here are persistent cloud "
            "storage — they survive between sessions and are accessible from "
            "any device on the network. You can browse the full vault with "
            "`hisar_list` and persist important deliverables with "
            "`hisar_deposit`. The Cell sandbox still isolates your commands "
            "from the host, but the workspace IS the vault: what you write "
            "here lives in Hisar."
        )
    elif has_hisar:
        text = (
            f"Your workspace is at {path_str}.\n\n"
            "H.İ.S.A.R. cloud storage is available in this session. Use "
            "`hisar_list` to browse the vault and `hisar_deposit` to persist "
            "files beyond this Cell — the vault survives; the workspace may "
            "not. The Cell sandbox isolates your commands from the host."
        )
    else:
        text = (
            f"Your workspace is at {path_str}.\n\n"
            "This is the project folder you were pointed at. Every shell "
            "command and file operation runs inside an isolated sandbox (your "
            "Cell), but the workspace is shared — files you write here are "
            "real files on disk, and they are what the operator sees."
        )

    return PromptFragment("shared:workspace", text)


def _turn_summary(terminal, session: Session, seconds: float) -> str:
    """The one line that closes a completed turn.

    Forge reported nothing at all on the successful path: the operator learned
    what a turn cost by running `/cost` afterwards, which in practice means
    never. Duration and iterations are the two numbers that change the next
    decision — whether to ask for less, and whether the loop is grinding.

    Context percentage rides along only once it is worth acting on. Below the
    warning line it is already on the status line above the prompt, and saying
    it twice per turn is how a number becomes furniture."""
    bits = [f"{_duration(seconds)}"]
    if terminal.iterations > 1:
        bits.append(f"{terminal.iterations} steps")
    usage = terminal.usage or {}
    out = usage.get("output", 0)
    if out:
        bits.append(f"{out:,} tokens out")
    fullness = usage.get("fullness") or 0
    if fullness >= status.WARN_FULLNESS:
        bits.append(f"{int(100 * fullness)}% context")
    return "  " + " · ".join(bits)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def _report(terminal, session: Session, verbose: bool, already_shown: bool = False,
            seconds: float = 0.0) -> None:
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
    elif seconds:
        ansi.write(ansi.paint(_turn_summary(terminal, session, seconds), "dim"))

    # Said once, on the crossing, and never again for this session unless the
    # context is reclaimed. The status line already carries the percentage on
    # every prompt; what it cannot do is mark the moment the number started
    # mattering, because a gauge that is always present is read as furniture.
    if (warning := status.pressure_warning(session)):
        ansi.write(ansi.paint(f"  ⚠ {warning}", "yellow"))

    # Last, so the terminal's attention flag lands after everything worth
    # reading is already on screen.
    notify.finished(seconds, _turn_summary(terminal, session, seconds).strip())
