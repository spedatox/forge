"""Shell execution tool. Runs inside the Cell only (§9.3) — the Warden never
touches the host shell."""
from __future__ import annotations

import re
import shlex

from pydantic import BaseModel, Field

from forge.warden.tool import (
    BACKSTOP_GRACE_S, Tool, ToolContext, ToolResult, cell_backed_timeout)

# Commands that observe and do not change anything. Kept deliberately short: this
# list decides whether a command may run alongside others and whether plan mode
# permits it, so every entry has to be one nobody would argue about. A command
# that is not on it is treated as a mutation, which costs a little parallelism
# and never costs correctness.
READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "tree",
    "pwd", "whoami", "id", "date", "uname", "hostname", "env", "printenv",
    "which", "type", "echo", "basename", "dirname", "realpath",
    "grep", "egrep", "fgrep", "rg", "find", "locate", "diff", "cmp", "sort",
    "uniq", "cut", "awk", "sed", "jq", "md5sum", "sha256sum",
    "python", "python3", "node",       # only with an inspection flag, see below
    "pip", "npm", "cargo", "go",       # only with an inspection subcommand
    "git",                             # only with an inspection subcommand
})

# For commands that are read-only in some moods and not others, the deciding
# token. `git status` observes; `git push` does not.
_READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "describe",
            "rev-parse", "blame", "shortlog", "config", "ls-files", "tag"},
    "pip": {"list", "show", "freeze", "check"},
    "npm": {"list", "ls", "view", "outdated"},
    "cargo": {"tree", "metadata"},
    "go": {"list", "env", "version"},
}

# An inspection flag makes an interpreter read-only; anything else runs code.
_READ_ONLY_FLAGS = {"--version", "-V", "--help", "-h"}

# Shell metacharacters that redirect or chain. Any of these and the command is
# doing more than the first token admits, so it is not classified at all.
_WRITES = re.compile(r"[>]|>>|\btee\b|\bdd\b")
_SEPARATORS = re.compile(r"[;&|]|\$\(|`")


def is_read_only_command(command: str) -> bool:
    """Best-effort, fail-closed: True only when the command is plainly harmless.

    Every ambiguity resolves to False. Redirection, chaining, substitution, an
    unparseable string, an unrecognized program — all mean "assume it writes".
    The cost of a false negative is a lost parallel slot; the cost of a false
    positive is two mutations racing on one workspace, so the asymmetry decides
    every judgement call here."""
    text = command.strip()
    if not text or _WRITES.search(text) or _SEPARATORS.search(text):
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:                      # unbalanced quotes
        return False
    if not tokens:
        return False

    program = tokens[0].rsplit("/", 1)[-1].removesuffix(".exe")
    if program not in READ_ONLY_COMMANDS:
        return False

    rest = [t for t in tokens[1:] if t not in ("--",)]
    subcommands = _READ_ONLY_SUBCOMMANDS.get(program)
    if subcommands is not None:
        first = next((t for t in rest if not t.startswith("-")), None)
        # `git` alone prints usage; `git status` observes; `git push` does not.
        return first is None or first in subcommands
    if program in {"python", "python3", "node"}:
        # An interpreter is only safe when it is not running anything.
        return bool(rest) and all(t in _READ_ONLY_FLAGS for t in rest)
    if program == "find":
        # find is read-only until -delete or -exec turns it into anything at all.
        return not any(t in {"-delete", "-exec", "-execdir", "-ok", "-fprint"} for t in rest)
    if program == "sed":
        return "-i" not in rest and not any(t.startswith("-i") for t in rest)
    return True


def _watcher(ctx: ToolContext):
    """The operator's view of a command in flight, or None if nobody is looking.

    Deliberately a plain callback into `ctx.on_command_output` rather than an
    emit through the event channel. Everything on that channel is part of the
    job record — it is what Mark VI replays and what the transcript is built
    from — and a build's console output is not a job record, it is a person
    looking over the agent's shoulder. Putting it on the channel would mean
    every remote consumer had to learn to ignore it.

    None when no surface registered one, which is the headless case: a
    dispatched job has no audience, so the streaming costs nothing and the
    backend skips the callback entirely."""
    return getattr(ctx, "on_command_output", None)


class RunCommandArgs(BaseModel):
    command: str = Field(description="The shell command to run inside the sandbox.")
    timeout: int | None = Field(
        default=None,
        description=(
            "Per-command wall-clock limit in seconds. Omit for the workspace "
            "default, which is short enough that a full test suite, a cold "
            "build, or a long scan can exceed it. Raise it for work you expect "
            "to be slow — the ceiling is well above the default, and asking for "
            "more time up front is far cheaper than being killed at the default "
            "and having to start the work again."))


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside your isolated sandbox and get back its stdout, "
        "stderr, and exit code. Use this to build, run tests, execute scripts, install "
        "packages, or inspect the environment. It is NOT read-only and NOT safe to run "
        "in parallel — commands can mutate the workspace, so they run one at a time. "
        "High-blast-radius commands (force-push, recursive delete, piping a download to "
        "a shell) are stopped by the safety gate unless the operator allow-lists them."
    )
    Args = RunCommandArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False       # individual dangerous commands are caught by the gate

    def timeout_s(self, args: RunCommandArgs, ctx: ToolContext) -> float:
        """Deliberately ABOVE this Cell's own ceiling, and read from it.

        The Cell is the timeout that should fire: it kills the process group, so
        the command actually stops, and it reports the honest cause. The
        dispatch deadline is only for a Cell that failed to honour its own — a
        wedged `docker exec`, a `wait_for` a bug did not arm. Ordering them the
        other way round would cut off legitimate long commands and report the
        wrong reason for it.

        Sized against `max_timeout_s`, not the profile default, because the
        model may ask for any timeout up to the ceiling on any given call.

        One case has no ordering at all: when the Cell does not stop commands by
        itself, neither may the dispatcher. A backstop that killed what the
        operator was deliberately letting run would take the decision back the
        moment they looked away, which is the opposite of what clearing
        `kill_on_timeout` asks for."""
        if not ctx.cell.policy.kill_on_timeout:
            return SELF_BOUNDED
        return cell_backed_timeout(ctx, per_run=ctx.cell.policy.max_timeout_s)

    def is_read_only(self, args: RunCommandArgs) -> bool:
        """Whether this particular command only observes.

        A shell tool that answers for its worst case makes every `git status`
        as expensive as an `rm -rf`: it serializes behind other work and plan
        mode refuses it. Answering per command is what lets a batch of
        inspections actually be a batch."""
        return is_read_only_command(args.command)

    def is_concurrency_safe(self, args: RunCommandArgs) -> bool:
        # Two observations cannot interfere. Anything that might write shares
        # one workspace with everything else and runs alone.
        return is_read_only_command(args.command)

    async def call(self, args: RunCommandArgs, ctx: ToolContext) -> ToolResult:
        res = await ctx.cell.run(args.command, timeout=args.timeout,
                                 env={} if ctx.network_allowed else None,
                                 on_output=_watcher(ctx))
        parts = [f"exit_code: {res.exit_code}"]
        if res.timed_out:
            parts.append("(timed out)")
        if res.stdout:
            parts.append(f"stdout:\n{res.stdout}")
        if res.stderr:
            parts.append(f"stderr:\n{res.stderr}")
        if res.timed_out:
            parts.append(self._timeout_advice(args, ctx))
        body = "\n".join(parts)
        return ToolResult(body, is_error=res.exit_code != 0)

    def _timeout_advice(self, args: RunCommandArgs, ctx: ToolContext) -> str:
        """What to do about a command the Cell killed.

        Without this the model is handed `exit_code: 124` and a line saying the
        command timed out, and nothing at all about the one lever that would fix
        it. The observed behaviour is that it re-runs the identical command,
        waits out the identical budget, and then reports the work as blocked —
        which reads to the operator as the agent getting confused, when in fact
        it was never told the budget was raisable or what it currently is.

        The numbers are read from the live policy rather than written down,
        because the default is per-agent (Optimus 120s, Centurion 300s) and a
        hard-coded figure would be wrong on one of them."""
        policy = ctx.cell.policy
        ceiling = policy.max_timeout_s
        # Clamped the way the Cell clamps it. Reporting the number the model
        # ASKED for would name a limit that was never in force — a `timeout` of
        # 9999 is silently capped, and telling the model it had 9999 seconds
        # teaches it the command is far slower than it is.
        used = min(args.timeout or policy.default_timeout_s, ceiling)
        if used >= ceiling:
            return (f"\nThis command hit the {ceiling}s ceiling, which is the most "
                    f"this workspace allows for a single command — a longer timeout "
                    f"is not available, so asking for one will not help. Split the "
                    f"work into stages that each finish inside it: a subset of the "
                    f"tests rather than the suite, one build target rather than a "
                    f"clean build, a narrower scan. Anything the command had already "
                    f"done before it was killed is still done.")
        return (f"\nThe limit was {used}s. It is not fixed: pass `timeout` up to "
                f"{ceiling} on this tool to give the command longer. If you expect "
                f"this work to be slow — a full suite, a cold build, a wide scan — "
                f"ask for the time up front rather than re-running the same command "
                f"at the same limit. Anything the command had already done before it "
                f"was killed is still done; check the state before redoing it.")
