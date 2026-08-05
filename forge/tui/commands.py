"""Slash commands — a registry, like every other extension point in Forge.

The reference harness has 101 of these. Forge has the dozen that answer a
question you cannot otherwise answer from inside a session: what is this costing,
how full is the window, what can the agent reach, what did the operator already
approve.

Each command returns a `CommandResult` rather than printing. That keeps them
testable without a terminal, and it is what lets `/compact` hand work back to the
session instead of reaching into it — a command that printed would have to know
about rendering, and a command that mutated state directly would have to know
about the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from forge.tui.session import Session


@dataclass
class CommandResult:
    """What a command wants done. `text` is shown; the flags ask the session for
    something only the session can do."""
    text: str = ""
    quit: bool = False
    clear: bool = False
    compact: bool = False


@dataclass
class Command:
    name: str
    summary: str                 # one line, shown by /help
    run: "Callable[[str, Session], Awaitable[CommandResult]]"
    aliases: tuple[str, ...] = ()


REGISTRY: dict[str, Command] = {}


def register(command: Command) -> Command:
    """Add a command. Later registration wins for a name — the registry is
    ordinary state, and a caller replacing a builtin has said what they meant."""
    REGISTRY[command.name] = command
    for alias in command.aliases:
        REGISTRY[alias] = command
    return command


def resolve(line: str) -> "tuple[Command | None, str]":
    """Split `/name rest` into its command and argument string."""
    body = line[1:].strip()
    if not body:
        return None, ""
    name, _, rest = body.partition(" ")
    return REGISTRY.get(name.lower()), rest.strip()


def command(name: str, summary: str, *aliases: str):
    def wrap(fn):
        register(Command(name=name, summary=summary, run=fn, aliases=aliases))
        return fn
    return wrap


# ── The commands ─────────────────────────────────────────────────────────────
@command("help", "list these commands", "?", "h")
async def _help(args: str, session: "Session") -> CommandResult:
    seen: dict[str, Command] = {}
    for cmd in REGISTRY.values():
        seen[cmd.name] = cmd
    width = max(len(c.name) for c in seen.values()) + 2
    lines = [f"  /{c.name:<{width}}{c.summary}" for c in sorted(seen.values(), key=lambda c: c.name)]
    return CommandResult("\n".join(lines) + "\n\n  Ctrl-C interrupts a running turn; Ctrl-D exits.")


@command("exit", "leave the session", "quit", "q")
async def _exit(args: str, session: "Session") -> CommandResult:
    return CommandResult(quit=True)


@command("clear", "forget the conversation and start fresh")
async def _clear(args: str, session: "Session") -> CommandResult:
    return CommandResult(clear=True)


@command("compact", "summarize the conversation now, freeing context")
async def _compact(args: str, session: "Session") -> CommandResult:
    if not session.messages:
        return CommandResult("Nothing to compact yet.")
    return CommandResult(compact=True)


@command("cost", "what this session has spent")
async def _cost(args: str, session: "Session") -> CommandResult:
    led = session.ledger
    if not led.turns:
        return CommandResult("No model turns yet.")
    lines = [
        f"  turns          {led.turns}",
        f"  input          {led.input_tokens:,} uncached",
        f"  output         {led.output_tokens:,}",
        f"  cache read     {led.cache_read_tokens:,}",
        f"  cache written  {led.cache_write_tokens:,}",
    ]
    if led.estimated:
        # Never present a guess as a measurement.
        lines.append("\n  These are ESTIMATES — this provider does not report usage.")
    return CommandResult("\n".join(lines))


@command("context", "how full the window is, and what is in it", "ctx")
async def _context(args: str, session: "Session") -> CommandResult:
    led = session.ledger
    used, limit = led.prompt_tokens, led.effective_limit
    pct = int(100 * used / max(1, limit))
    filled = int(28 * used / max(1, limit))
    bar = "█" * min(28, filled) + "░" * max(0, 28 - filled)
    lines = [
        f"  [{bar}] {pct}%",
        f"  {used:,} of {limit:,} usable tokens"
        + ("  (estimated)" if led.estimated else ""),
        f"  compaction at  {led.compact_at:,}",
        f"  messages       {len(session.messages)}",
    ]
    if led.should_compact():
        lines.append("\n  Over the threshold — the next turn will compact first.")
    return CommandResult("\n".join(lines))


@command("tools", "what the agent can reach")
async def _tools(args: str, session: "Session") -> CommandResult:
    if not session.tools:
        return CommandResult("No tools.")
    width = max(len(n) for n in session.tools) + 2
    lines = []
    for name in sorted(session.tools):
        tool = session.tools[name]
        marks = []
        if tool.READ_ONLY:
            marks.append("read-only")
        if tool.CONCURRENCY_SAFE:
            marks.append("parallel")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(f"  {name:<{width}}{ansi_first_sentence(tool.description)}{suffix}")
    return CommandResult("\n".join(lines))


def ansi_first_sentence(text: str, limit: int = 60) -> str:
    head = text.split(". ")[0].strip()
    return head if len(head) <= limit else head[: limit - 1] + "…"


@command("model", "which model this session uses")
async def _model(args: str, session: "Session") -> CommandResult:
    return CommandResult(f"  {session.model_ref}\n  window {session.ledger.context_limit:,} tokens")


@command("agent", "the agent profile in use")
async def _agent(args: str, session: "Session") -> CommandResult:
    cfg = session.cfg
    return CommandResult(
        f"  {cfg.agent_id} — {cfg.name}\n"
        f"  domain     {cfg.domain}\n"
        f"  mode       {cfg.permission_mode}\n"
        f"  max iters  {cfg.max_iterations}")


@command("approved", "operations you have permanently approved")
async def _approved(args: str, session: "Session") -> CommandResult:
    entries = sorted(session.allowlist.entries)
    if not entries:
        return CommandResult("Nothing approved yet. Gated operations will ask.")
    return CommandResult("\n".join(f"  {e}" for e in entries)
                         + f"\n\n  Stored in {session.allowlist.path or '(memory only)'}")


@command("transcript", "dump the raw conversation")
async def _transcript(args: str, session: "Session") -> CommandResult:
    from forge.warden.compaction import render_for_summary

    if not session.messages:
        return CommandResult("Empty.")
    return CommandResult(render_for_summary(session.messages))


@command("cwd", "which directory the agent is working in")
async def _cwd(args: str, session: "Session") -> CommandResult:
    return CommandResult(f"  {session.workspace}")


def command_help() -> dict[str, str]:
    """{name: summary} for every registered command — what the input bar's
    completer offers. Built from the registry so a new command shows up in
    completion by existing, with no second list to keep in step."""
    return {c.name: c.summary for c in REGISTRY.values()}


# ── Git: what the agent actually changed ─────────────────────────────────────
# The single most common question after a turn is "what did it do to my repo",
# and the honest answer is git's, not the agent's. These run through the Cell so
# they see the same working directory the agent does — including an active
# worktree, where the operator's own `git diff` in another terminal would show
# nothing at all.


async def _cell_git(session: "Session", command: str) -> tuple[str, bool]:
    """(output, ok). Never raises: a REPL command must not end the session."""
    if session.cell is None:
        return "No Cell attached.", False
    try:
        res = await session.cell.run(command, timeout=30)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}", False
    body = (res.stdout or "").rstrip() or (res.stderr or "").rstrip()
    return body, res.exit_code == 0


@command("diff", "what has changed in the working tree")
async def _diff(args: str, session: "Session") -> CommandResult:
    from forge.tui.render import _paint_diff_line

    target = args.strip() or ""
    out, ok = await _cell_git(session, f"git diff --stat {target}".strip())
    if not ok:
        return CommandResult(f"  {out}")
    if not out:
        return CommandResult("  Working tree clean.")

    full, _ = await _cell_git(session, f"git diff {target}".strip())
    lines = [f"  {ln}" for ln in out.splitlines()]
    if full:
        lines.append("")
        lines.extend("  " + _paint_diff_line(ln) for ln in full.splitlines()[:200])
        if len(full.splitlines()) > 200:
            lines.append(ansi_dim("  … truncated — run `git diff` for the rest"))
    return CommandResult("\n".join(lines))


@command("status", "git state and where this session is", "st")
async def _status_cmd(args: str, session: "Session") -> CommandResult:
    from forge.tui import status as status_mod

    status_mod.forget_branch(session.workspace)   # /status should re-read, not cache
    lines = [
        f"  agent      {session.cfg.agent_id}",
        f"  model      {session.model_ref}",
        f"  mode       {session.permission_mode}",
        f"  workspace  {session.workspace}",
    ]
    if session.cell is not None and getattr(session.cell, "subpath", ""):
        lines.append(f"  worktree   {session.cell.subpath}")
    lines.append(f"  turns      {session.turns}")

    out, ok = await _cell_git(session, "git status --short --branch")
    lines.append("")
    if ok:
        lines.extend(f"  {ln}" for ln in (out.splitlines() or ["working tree clean"]))
    else:
        # Report what actually failed. Collapsing every failure to "not a git
        # repository" tells the operator something false about their repo when
        # the real problem is that the Cell is down — and that sends them
        # looking in exactly the wrong place.
        lines.extend(f"  {ln}" for ln in (out or "git is unavailable here").splitlines())
    return CommandResult("\n".join(lines))


@command("branch", "which branch, or switch to another")
async def _branch(args: str, session: "Session") -> CommandResult:
    from forge.tui import status as status_mod

    name = args.strip()
    if not name:
        out, ok = await _cell_git(session, "git branch --show-current")
        return CommandResult(f"  {out or 'detached or not a repository'}")
    out, ok = await _cell_git(session, f"git checkout {name}")
    status_mod.forget_branch(session.workspace)
    return CommandResult(f"  {out}")


# ── Diagnostics ──────────────────────────────────────────────────────────────


@command("doctor", "check that this environment can actually run a turn")
async def _doctor(args: str, session: "Session") -> CommandResult:
    """Every check answers a question that otherwise only surfaces mid-turn, as
    a confusing failure. Cheap to run, and each line names what to do about it."""
    import os
    import shutil

    from forge.tui.input import AVAILABLE as INPUT_RICH

    def mark(ok: bool) -> str:
        return "ok  " if ok else "MISS"

    provider = session.model_ref.split(":", 1)[0] if ":" in session.model_ref else "anthropic"
    key_env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
               "gemini": "GEMINI_API_KEY", "zai": "ZAI_API_KEY",
               "deepseek": "DEEPSEEK_API_KEY"}.get(provider, "ANTHROPIC_API_KEY")

    rows = [
        (bool(os.environ.get(key_env)), f"{key_env} set", f"the model {session.model_ref} cannot run without it"),
        (session.cell is not None, "Cell attached", "no sandbox — run_command and file tools are dead"),
        (bool(shutil.which("git")), "git on PATH", "worktrees, /diff and /status need it"),
        (INPUT_RICH, "rich input line", "history and completion are off; pip install prompt_toolkit"),
        (bool(os.environ.get("TAVILY_API_KEY")), "TAVILY_API_KEY set", "web_search will refuse"),
        (bool(session.tools), "tools registered", "the agent has nothing to work with"),
    ]
    lines = [f"  [{mark(ok)}] {label}" + ("" if ok else f"\n         → {why}")
             for ok, label, why in rows]

    out, git_ok = await _cell_git(session, "git rev-parse --is-inside-work-tree")
    lines.append(f"  [{mark(git_ok)}] workspace is a git repository"
                 + ("" if git_ok else "\n         → worktree isolation is unavailable here"))
    return CommandResult("\n".join(lines))


@command("keybindings", "the keys this REPL understands", "keys")
async def _keys(args: str, session: "Session") -> CommandResult:
    rows = [
        ("↑ / ↓", "walk the history of what you typed here"),
        ("ctrl+r", "search that history"),
        ("esc then enter", "newline instead of submitting"),
        ("tab", "complete a /command or an @path"),
        ("shift+tab", "switch act ⇄ plan"),
        ("ctrl+o", "reprint the last shortened tool output in full"),
        ("ctrl+c", "interrupt the running turn (again at an empty prompt exits)"),
        ("ctrl+d", "end the session"),
        ("!cmd", "run a shell command with no model turn"),
        ("@path", "complete a file from this workspace"),
    ]
    width = max(len(k) for k, _ in rows)
    return CommandResult("\n".join(f"  {k:<{width}}   {v}" for k, v in rows))


# ── Getting the conversation out ─────────────────────────────────────────────


@command("export", "write this conversation to a file")
async def _export(args: str, session: "Session") -> CommandResult:
    import json
    from datetime import datetime

    if not session.messages:
        return CommandResult("  Nothing to export yet.")
    name = args.strip() or f"forge-{datetime.now():%Y%m%d-%H%M%S}.json"
    target = session.workspace / name
    try:
        target.write_text(json.dumps(session.messages, indent=2, default=str),
                          encoding="utf-8")
    except OSError as e:
        return CommandResult(f"  Could not write {name}: {e}")
    return CommandResult(f"  Wrote {len(session.messages)} messages to {target}")


def ansi_dim(text: str) -> str:
    from forge.tui import ansi
    return ansi.paint(text, "dim")
