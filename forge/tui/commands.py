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
    from forge.model.catalog import ProviderCatalog
    from forge.tui.picker import Group
    from forge.tui.session import Session


@dataclass
class CommandResult:
    """What a command wants done. `text` is shown; the flags ask the session for
    something only the session can do."""
    text: str = ""
    quit: bool = False
    clear: bool = False
    compact: bool = False
    prompt: str = ""
    """Run this as a normal turn after showing `text`.

    Lets a command hand work to the model without knowing anything about the
    loop — /review is a diff plus a request to read it, and writing that by
    hand every time is exactly the friction that stops it happening."""


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


@command("checkpoint", "snapshot the conversation so you can rewind to here")
async def _checkpoint(args: str, session: "Session") -> CommandResult:
    import copy
    from datetime import datetime

    if not session.messages:
        return CommandResult("  Nothing to checkpoint yet — the conversation is empty.")
    cp = {
        "turn": session.turns,
        "messages": copy.deepcopy(session.messages),
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    }
    session.checkpoints.append(cp)
    n = len(session.checkpoints)
    return CommandResult(
        f"  Checkpoint {n} saved — turn {session.turns}, "
        f"{len(session.messages)} messages.  /restore {n} to rewind here.")


@command("restore", "rewind the conversation to an earlier checkpoint")
async def _restore(args: str, session: "Session") -> CommandResult:
    arg = args.strip()
    if not session.checkpoints:
        return CommandResult("  No checkpoints. Use /checkpoint first, then /restore to rewind.")

    if not arg:
        # Restore to the latest checkpoint.
        cp = session.checkpoints[-1]
        session.messages = cp["messages"]
        session.turns = cp["turn"]
        # Discard all checkpoints after the one we restored to — they describe
        # turns that no longer exist.
        return CommandResult(
            f"  Restored to checkpoint {len(session.checkpoints)}"
            f" (turn {cp['turn']}, {cp['timestamp']}).")

    # /restore N — restore to a specific checkpoint index.
    try:
        n = int(arg)
    except ValueError:
        return CommandResult(f"  {arg!r} is not a checkpoint number. /checkpoints to list them.")
    if n < 1 or n > len(session.checkpoints):
        return CommandResult(
            f"  {n} is not a valid checkpoint. You have {len(session.checkpoints)}"
            f" — /checkpoints to list them.")

    cp = session.checkpoints[n - 1]
    session.messages = cp["messages"]
    session.turns = cp["turn"]
    # Discard checkpoints after the restored one.
    session.checkpoints = session.checkpoints[:n - 1]
    return CommandResult(
        f"  Restored to checkpoint {n} (turn {cp['turn']}, {cp['timestamp']})."
        f"\n  Later checkpoints were discarded — the turns they captured are gone.")


@command("checkpoints", "list saved checkpoints", "cps")
async def _checkpoints_cmd(args: str, session: "Session") -> CommandResult:
    if not session.checkpoints:
        return CommandResult("  No checkpoints yet. /checkpoint to save one.")
    lines = []
    for i, cp in enumerate(session.checkpoints, 1):
        lines.append(
            f"  {i}. turn {cp['turn']} · {cp['timestamp']}"
            f" · {len(cp['messages'])} messages")
    lines.append(f"\n  /restore {len(session.checkpoints)} to rewind to the latest.")
    return CommandResult("\n".join(lines))


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


@command("model", "switch model — pick one, or /model <ref>", "models")
async def _model(args: str, session: "Session") -> CommandResult:
    """Bare `/model` opens the picker; a ref switches straight to it.

    The list comes from the providers themselves rather than from a table here,
    and only from the ones whose key is set — see `forge/model/catalog.py`. A
    typed ref is NOT validated against that list: a model released this morning
    should be usable this morning, and the provider's own error is a better
    referee than a cached list would be.

    The switch is for this session only. The profile's `model_ref` is a
    deployment fact and stays where it is; `--model` at launch is the way to
    mean it every time.
    """
    from forge.config import ForgeSettings, load_env_files
    from forge.model.catalog import PROVIDER_KEY_ENV, catalog, invalidate
    from forge.tui import picker

    arg = args.strip()
    if arg in ("show", "which", "?"):
        return CommandResult(f"  {session.model_ref}\n"
                             f"  window {session.ledger.context_limit:,} tokens")
    if arg and arg not in ("refresh", "reload"):
        return _switch_to(session, arg)

    if arg in ("refresh", "reload"):
        # Re-read .env as well as dropping the cache: the operator who reaches
        # for refresh has usually just pasted a key into it, and "restart the
        # session" is a poor answer to that.
        load_env_files()
        invalidate()
    settings = ForgeSettings.from_env()
    groups = await catalog(settings)
    if not groups:
        keys_wanted = ", ".join(sorted(set(PROVIDER_KEY_ENV.values())))
        return CommandResult(
            f"  {session.model_ref}\n"
            "  No provider is configured, so there is nothing to choose between.\n"
            f"  Set one of {keys_wanted} in .env or ~/.forge/.env.")

    if not any(c.models for c in groups):
        # Every configured provider failed or answered empty. The picker would
        # open on nothing and close again, and "still on the old model" is a
        # true sentence that hides the only thing worth reading — which key is
        # wrong, or which daemon is not running.
        lines = [f"  {session.model_ref}",
                 "  No provider could list its models:"]
        for cat in groups:
            lines.append(f"    {cat.provider:<10} {cat.error or 'returned nothing'}")
        return CommandResult("\n".join(lines))

    chosen = await picker.pick(
        [_provider_group(c, session.model_ref) for c in groups],
        title="model", current=session.model_ref)
    if chosen is None:
        return CommandResult(f"  Still {session.model_ref}.")
    return _switch_to(session, chosen)


def _provider_group(cat: "ProviderCatalog", current: str) -> "Group":
    """One provider's models as a picker group.

    The heading carries the count and, when the filter dropped anything, how
    much it dropped — an operator looking for an embedding model needs to see
    that the list is not everything the endpoint said.
    """
    from forge.tui.picker import Group, Option

    title = cat.provider
    if cat.models:
        title += f"   {len(cat.models)} models"
        if cat.hidden:
            title += f" · {cat.hidden} non-chat hidden"
    return Group(
        title=title,
        options=tuple(Option(value=m.ref,
                             label=m.model_id,
                             hint="· current" if m.ref == current else m.label)
                      for m in cat.models),
        note=cat.error,
    )


def _switch_to(session: "Session", ref: str) -> CommandResult:
    """Point the session at `ref`. Takes effect on the next turn, because that
    is when the model is built — nothing in flight is disturbed."""
    previous = session.model_ref
    if ref == previous:
        return CommandResult(f"  Already on {ref}.")
    session.model_ref = ref
    return CommandResult(f"  {previous}  →  {ref}\n"
                         "  Takes effect on the next turn; history carries over.")


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


@command("transcript", "page through the raw conversation")
async def _transcript(args: str, session: "Session") -> CommandResult:
    """Paged rather than dumped.

    Printing a long conversation into scrollback buries the conversation under a
    copy of itself, and takes the operator's own scrollback — the thing they
    would have scrolled to find this — with it. `pager.page` prints only what
    was asked for and stops on `q`, so what is left behind is what they chose to
    read. Returns empty text because the pager has already written."""
    from forge.tui import pager
    from forge.warden.compaction import render_for_summary

    if not session.messages:
        return CommandResult("Empty.")
    await pager.page(render_for_summary(session.messages), title="transcript")
    return CommandResult("")


@command("cwd", "which directory the agent is working in")
async def _cwd(args: str, session: "Session") -> CommandResult:
    return CommandResult(f"  {session.workspace}")


@command("mkdir", "create a directory inside the workspace")
async def _mkdir(args: str, session: "Session") -> CommandResult:
    name = args.strip()
    if not name:
        return CommandResult("  /mkdir <path> — the directory to create, relative to the workspace.")
    target = (session.workspace / name).resolve()
    # Refuse to create outside the workspace — `/mkdir ../escape` must not
    # reach the parent directory, even though the Cell cannot either.
    try:
        target.relative_to(session.workspace)
    except ValueError:
        return CommandResult(f"  {name!r} is outside the workspace — refused.")
    already = target.is_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return CommandResult(f"  Could not create {name}: {e}")
    # Invalidate the @mention file cache so completions pick up the new directory.
    bar = session.input_bar
    if bar is not None and hasattr(bar, "invalidate_file_cache"):
        bar.invalidate_file_cache()
    if already:
        return CommandResult(f"  {name} already exists.\n  {target}")
    return CommandResult(f"  Created {name}\n  {target}")


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
        ("ctrl+x ctrl+e", "open $EDITOR on what you are typing"),
        ("tab", "complete a /command or an @path"),
        ("shift+tab", "switch act / plan"),
        ("ctrl+o", "reprint the last shortened tool output in full"),
        ("ctrl+c", "interrupt the running turn (again at an empty prompt exits)"),
        ("ctrl+d", "end the session"),
        ("type while it works", "steer the run — picked up at the next step"),
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


# ── Editing, clipboard, and what is configured ───────────────────────────────


@command("vim", "vi keys in the input line, or back to emacs")
async def _vim(args: str, session: "Session") -> CommandResult:
    from forge.tui.input import AVAILABLE

    bar = session.input_bar
    if bar is None or not getattr(bar, "_session", None):
        # Two different causes, and telling the operator to install a package
        # they already have sends them to fix the wrong thing.
        return CommandResult(
            "  No line editor is active, so there are no vi keys to switch to.\n"
            + ("  prompt_toolkit is installed but could not drive this terminal —\n"
               "  this happens with piped input, and under Git Bash on Windows."
               if AVAILABLE else
               "  Install the `tui` extra: pip install -e \".[tui]\""))
    want = {"on": True, "off": False}.get(args.strip().lower(), not bar.vi_mode)
    now = bar.set_vi_mode(want)
    return CommandResult(f"  Input mode: {'vi' if now else 'emacs'}")


def _clipboard_command() -> list[str] | None:
    """The platform's "read stdin into the clipboard" command, if there is one.

    Done with the tool the OS already ships rather than a dependency: pyperclip
    would be a third package to install, on every platform, to move one string.
    """
    import shutil
    import sys

    if sys.platform == "win32":
        return ["clip"]
    if sys.platform == "darwin":
        return ["pbcopy"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def _last_assistant_text(session: "Session") -> str:
    for message in reversed(session.messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(parts):
                return "\n".join(p for p in parts if p)
    return ""


@command("copy", "put the last reply on the clipboard")
async def _copy(args: str, session: "Session") -> CommandResult:
    import subprocess

    text = _last_assistant_text(session)
    if not text:
        return CommandResult("  Nothing to copy yet.")
    argv = _clipboard_command()
    if argv is None:
        return CommandResult(
            "  No clipboard tool found. Install wl-clipboard, xclip or xsel, "
            "or use /export to write the conversation to a file.")
    try:
        subprocess.run(argv, input=text.encode("utf-8"), check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return CommandResult(f"  Clipboard failed ({argv[0]}): {e}")
    return CommandResult(f"  Copied {len(text)} characters.")


@command("mcp", "MCP servers and the tools they contributed")
async def _mcp(args: str, session: "Session") -> CommandResult:
    """MCP tools are named mcp__{server}__{tool}, so the registry itself says
    which server contributed what — no second source to fall out of step."""
    servers: dict[str, list[str]] = {}
    for name in session.tools:
        if not name.startswith("mcp__"):
            continue
        _, _, rest = name.partition("mcp__")
        server, _, tool = rest.partition("__")
        servers.setdefault(server, []).append(tool or rest)

    if not servers:
        return CommandResult(
            "  No MCP servers connected.\n"
            "  Configure them in .forge/mcp.json; tools appear as mcp__<server>__<tool>.")
    lines = []
    for server, tools in sorted(servers.items()):
        lines.append(f"  {server}  ({len(tools)} tools)")
        lines.extend(f"      {t}" for t in sorted(tools))
    return CommandResult("\n".join(lines))


@command("permissions", "what is gated, and what you have allowed", "perms")
async def _permissions(args: str, session: "Session") -> CommandResult:
    allow = session.allowlist
    lines = [
        f"  mode          {session.permission_mode}"
        + ("   (every mutating operation is denied)"
           if session.permission_mode == "plan" else ""),
        f"  allowlist     {allow.path or '(not persisted)'}",
        "",
    ]
    entries = sorted(getattr(allow, "entries", ()) or ())
    if entries:
        lines.append("  Approved in advance:")
        lines.extend(f"      {e}" for e in entries)
    else:
        lines.append("  Nothing approved in advance.")
    lines += [
        "",
        "  High-impact operations — force pushes, recursive deletes, piping a",
        "  download into a shell — are always asked about and cannot be",
        "  pre-approved here. Use shift+tab for plan mode to deny every",
        "  mutation for a while.",
    ]
    return CommandResult("\n".join(lines))


# ── Committing and reviewing ─────────────────────────────────────────────────


@command("commit", "stage everything and commit")
async def _commit(args: str, session: "Session") -> CommandResult:
    """Commits through the CELL, so the work is attributed to the agent that
    did it — the Cell carries the agent's git identity (agents/config.py).
    Running this on the host instead would record the operator as the author of
    code they did not write."""
    status, ok = await _cell_git(session, "git status --porcelain")
    if not ok:
        return CommandResult(f"  {status}")
    if not status.strip():
        return CommandResult("  Nothing to commit.")

    message = args.strip()
    if not message:
        changed = len(status.splitlines())
        return CommandResult(
            f"  {changed} file(s) changed. A commit needs a message:\n"
            "      /commit <what changed and why>\n\n"
            "  Ask the agent to write one with /review first if you want a hand.")

    staged, ok = await _cell_git(session, "git add -A")
    if not ok:
        return CommandResult(f"  Could not stage: {staged}")

    # -- to stop a message beginning with a dash being read as a flag, and
    # single quotes doubled so the shell keeps the message intact.
    safe = message.replace("'", "'\''")
    out, ok = await _cell_git(session, f"git commit -m '{safe}'")
    if not ok:
        return CommandResult(f"  Commit failed:\n  {out}")

    who, _ = await _cell_git(session, "git log -1 --format=%an")
    return CommandResult(f"  {out}\n  Authored by {who.strip() or 'unknown'}.")


@command("review", "have the agent read its own uncommitted changes")
async def _review(args: str, session: "Session") -> CommandResult:
    """The one review that reliably does not happen is the one requiring the
    operator to write the request. This assembles it: the diff is already on
    disk, so the command supplies the ask and the agent reads what it wrote."""
    stat, ok = await _cell_git(session, "git diff --stat HEAD")
    if not ok:
        return CommandResult(f"  {stat}")
    if not stat.strip():
        return CommandResult("  Nothing to review — the working tree is clean.")

    focus = args.strip()
    ask = (
        "Review the uncommitted changes in this repository. Run "
        "`git diff HEAD` to read them, then report: anything that looks wrong "
        "or unfinished, anything that changes behaviour beyond what was asked, "
        "and anything missing a test. Be specific — name files and lines. If it "
        "looks correct, say so plainly rather than inventing concerns."
    )
    if focus:
        ask += f"\n\nPay particular attention to: {focus}"
    return CommandResult(
        "\n".join(f"  {ln}" for ln in stat.splitlines()) + "\n", prompt=ask)


# ── Picking a conversation back up ───────────────────────────────────────────


@command("plugins", "what the plugin manifest loaded, and what failed")
async def _plugins(args: str, session: "Session") -> CommandResult:
    """Loaded is not the same as attached.

    A plugin whose `apply` ran but registered nothing looks identical to a
    working one from the outside — the manifest names it, the log says loaded,
    and it does nothing. So this reports the EVENTS each plugin is actually on
    and the tools it actually contributed, which is the difference between
    "installed" and "in effect". Failures are listed with their reason rather
    than omitted, because a plugin silently missing is the thing an operator
    spends longest not noticing."""
    plugins = getattr(getattr(session, "extensions", None), "plugins", None)
    if plugins is None:
        return CommandResult(
            "  No plugins loaded.\n"
            "  Add a \"plugins\" list to .forge/extensions.json — see\n"
            "  forge/plugins/__init__.py for the contract.")
    rows = plugins.describe()
    if not rows:
        return CommandResult("  The plugin manifest is empty.")
    return CommandResult("\n".join(["  Plugins:", *rows]))


@command("sessions", "conversations you can resume in this workspace", "ls")
async def _sessions(args: str, session: "Session") -> CommandResult:
    from forge.tui import persistence

    entries = persistence.listing(session.workspace)
    if not entries:
        return CommandResult(
            "  No saved conversations here yet. They are written after each\n"
            "  turn, into .forge/sessions/.")
    lines = []
    for i, e in enumerate(entries, 1):
        current = "  (this one)" if e.id == session.session_id else ""
        lines.append(f"  {i:>2}. {e.title}")
        lines.append(f"      {e.agent_id} · {e.turns} turns · {e.age} · {e.id}{current}")
    lines.append("")
    lines.append("  /resume <number|id> to pick one up.")
    return CommandResult("\n".join(lines))


@command("resume", "continue an earlier conversation")
async def _resume(args: str, session: "Session") -> CommandResult:
    """Replaces the live transcript with a stored one.

    The Cell, the tools and the allowlist are NOT restored — they are rebuilt
    from the current profile and the current workspace, which is the honest
    thing to do: a conversation from last week describes files as they were,
    and pretending otherwise would let the model edit against remembered text.
    Read-tracking starts empty for the same reason, so the first edit after a
    resume has to read the file again.
    """
    from forge.tui import persistence

    entries = persistence.listing(session.workspace)
    if not entries:
        return CommandResult("  Nothing to resume in this workspace.")

    picked = persistence.resolve(session.workspace, args)
    if picked is None:
        return CommandResult(
            f"  No session matches {args.strip()!r}. Try /sessions for the list.")
    if picked.id == session.session_id:
        return CommandResult("  That is the conversation you are already in.")

    messages = persistence.load(session.workspace, picked.id)
    if messages is None:
        return CommandResult(f"  Could not read session {picked.id}.")

    session.messages = messages
    session.session_id = picked.id
    session.turns = picked.turns
    session.last_truncated = None
    return CommandResult(
        f"  Resumed: {picked.title}\n"
        f"  {picked.turns} turns, {len(messages)} messages, last touched {picked.age}.\n"
        "  Files may have changed since — the agent will re-read before editing.")


# ── Project conventions ──────────────────────────────────────────────────────


@command("init", "write an AGENTS.md so the agent knows this project's conventions")
async def _init(args: str, session: "Session") -> CommandResult:
    """Asks the agent to read the repository and write down its conventions.

    Without one, an agent rediscovers a project by being corrected: wrong test
    runner, wrong docstring style, a dependency the project deliberately
    avoids. Each costs a turn to hit and another to fix, every session.

    The agent writes it rather than a template, because a useful conventions
    file is specific to what is actually in the repository, and a generic one
    is worse than none — it occupies the same space on every turn while saying
    nothing.
    """
    from forge.agents import conventions

    existing = conventions.find(session.workspace)
    if existing is not None and args.strip().lower() != "force":
        rel = existing.relative_to(session.workspace)
        return CommandResult(
            f"  {rel} already exists and is loaded into every turn.\n"
            "  Edit it directly, or /init force to have the agent rewrite it.")

    target = conventions.FILENAMES[0]
    return CommandResult(
        f"  Asking the agent to survey this repository and write {target}.\n",
        prompt=(
            f"Write {target} for this repository: the conventions someone "
            "working here has to know, which are not obvious from a single "
            "file.\n\n"
            "First look: the build and dependency files, the test setup, the "
            "directory layout, and a few representative source files. Use "
            "glob, grep and read_file — do not guess from the directory names.\n\n"
            "Then write it, covering only what you actually verified:\n"
            "- what this project is, in two sentences\n"
            "- how to install, run and test it, with the real commands\n"
            "- the layout, and where a new module of each kind belongs\n"
            "- conventions visible in the code: naming, error handling, typing, "
            "test style, anything the project does consistently and unusually\n"
            "- anything deliberately avoided, where the code shows it\n\n"
            "Be specific and short. This file is sent to the model on every "
            "turn, so a sentence that does not change what an agent would do is "
            "costing money to say nothing. Do not include a licence, a feature "
            "list, or anything you could have written without reading the repo."
        ))
