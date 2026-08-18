"""SubprocessCell — reduced-isolation Cell backend for Docker-less dev / CI.

Same four-method contract as DockerCell, but the boundary is a per-agent
workspace directory plus wall-clock and output caps, not a kernel namespace.
It is honest about what it is: a workspace jail, NOT a security boundary against
hostile code. Use DockerCell in production (§8). The factory picks this only
when Docker is unavailable or explicitly requested.

Every command runs with cwd pinned to the workspace; read/write refuse paths
that escape it. Network cannot be portably severed from a plain subprocess, so
when the policy forbids network this backend sets proxy-blackhole env vars as a
best-effort deterrent and the README documents the gap.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path

from forge.cell.base import Cell, CellPolicy, CommandResult
from forge.cell.stream import REAP_GRACE_S, Retained, drain as drain_pipe


def _posix_shell() -> str | None:
    """A POSIX shell on this machine, or None if there is genuinely none.

    On Windows `create_subprocess_shell` means cmd.exe, so `mkdir -p`,
    `ls`, `rm -rf` and every other command an agent writes by reflex fail with
    "The syntax of the command is incorrect." The agent then has to guess which
    dialect it is talking to, on a per-command basis, from an error that does
    not say.

    Git ships bash on essentially every Windows dev machine, so preferring it
    means one shell everywhere: the same command works on the owner's laptop
    and on the Linux server, and nothing about the agent's habits has to fork
    by platform. Falling back to cmd.exe is deliberate — a working cmd is
    better than refusing to run at all — but the caller is told which it got.
    """
    if os.name != "nt":
        return None
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(candidate).exists():
            return candidate
    return None


def _own_process_group() -> dict:
    """Launch kwargs that put the command in a group of its own.

    Without this there is nothing to kill but the shell. `pytest`, `npm`, a dev
    server — everything an agent actually runs is a CHILD of that shell, and
    killing the parent leaves the children holding the port, the lock and the
    file handles. The command reports "timed out", the next one inherits a
    workspace that is still busy, and Forge looks stuck running a command it
    has not started yet.

    A group is also the only handle that survives the shell exiting first, so
    it is set at launch rather than looked up at kill time."""
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP. Named rather than imported from subprocess
        # so this module still reads on POSIX, where the constant is absent.
        return {"creationflags": 0x00000200}
    return {"start_new_session": True}


def _kill_tree(proc) -> None:
    """Kill the command and everything it started. Best-effort throughout.

    A process that has already exited, a group that is already gone, a
    `taskkill` that is not on PATH — none of those is a reason to fail the
    call, which is going to report a timeout either way. What matters is that
    the common case stops leaving processes behind."""
    pid = proc.pid
    try:
        if os.name == "nt":
            # /T is the whole point: it takes the tree, not just the shell.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=10)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 — see docstring
        pass
    try:
        proc.kill()                 # the shell itself, if it outlived the group
    except (ProcessLookupError, OSError):
        pass



def _release_pipes(proc) -> None:
    """Close the pipes the cancelled readers left open.

    Cancelling a reader abandons its transport with it, and asyncio complains
    about the unclosed pipe from a destructor later — at a point with no
    connection to the command that caused it. A long session times out more
    than one command, and a handle leaked per timeout is a slow one."""
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 — already closed, or never opened
        pass


class SubprocessCell(Cell):
    def __init__(self, workspace: Path, policy: CellPolicy) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self._shell = _posix_shell()

    @property
    def shell_dialect(self) -> str:
        """"posix" or "cmd" — what commands sent here are parsed as."""
        if os.name != "nt" or self._shell:
            return "posix"
        return "cmd"

    @property
    def workdir(self) -> Path:
        """Where commands run and paths resolve — the workspace, or the
        subdirectory an active worktree narrowed it to."""
        return (self.workspace / self._subpath) if self._subpath else self.workspace

    @property
    def host_path(self) -> Path:
        return self.workdir

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _base_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        # Never leak the operator's model/provider keys into executed code.
        for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                       "SPEDA_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY"):
            env.pop(secret, None)
        if not self.policy.allow_network:
            # Best-effort only (subprocess isolation cannot truly cut the network).
            env.update({"http_proxy": "http://127.0.0.1:9",
                        "https_proxy": "http://127.0.0.1:9",
                        "HTTP_PROXY": "http://127.0.0.1:9",
                        "HTTPS_PROXY": "http://127.0.0.1:9",
                        "no_proxy": ""})
        env.update(self.policy.env or {})
        if extra:
            env.update(extra)
        return env

    def _resolve(self, path: str) -> Path:
        # Validated against workdir, not workspace: inside a worktree the
        # boundary tightens with it, so `../main-checkout/x` is refused.
        root = self.workdir.resolve()
        target = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        if os.path.commonpath([str(target), str(root)]) != str(root):
            raise PermissionError(f"path escapes the Cell workspace: {path!r}")
        return target

    async def _await_drain(self, drain, t: int, on_output) -> None:
        """Wait for the command's output to end, honouring the kill policy.

        With `kill_on_timeout` set — the headless posture — this is a plain
        deadline and the caller kills what is left. Nobody is watching a
        dispatched job, so a command that never ends has to be ended for it.

        With it clear — the interactive posture — the deadline stops being a
        sentence and becomes a notice. The command keeps running, the operator
        is told it has passed its budget and how to stop it, and the decision is
        theirs. That is only safe because they can SEE it: the streaming above
        is what makes an unbounded command something you are watching rather
        than something you are waiting on. The notice repeats on the same
        interval, because one line printed five minutes ago has scrolled away.
        """
        if self.policy.kill_on_timeout:
            await asyncio.wait_for(drain, timeout=t)
            return

        elapsed = 0
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(drain), timeout=t)
                return
            except (asyncio.TimeoutError, TimeoutError):
                elapsed += t
                if on_output is None:
                    continue
                try:
                    on_output("stderr",
                              f"\n[still running after {elapsed}s — this workspace "
                              f"does not stop long commands by itself; press ctrl+c "
                              f"to stop it]\n")
                except Exception:  # noqa: BLE001 — a renderer fault is not the command's
                    on_output = None

    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None,
                  on_output=None) -> CommandResult:
        t = self._clamp_timeout(timeout)
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            if self._shell:
                # `bash -c` rather than the platform shell, so one dialect works
                # on the laptop and the server alike. exec_ (not shell_) because
                # the command is bash's argument, not something for cmd to parse
                # first — otherwise cmd mangles the quoting on the way through.
                proc = await asyncio.create_subprocess_exec(
                    self._shell, "-c", command,
                    cwd=str(self.workdir),
                    env=self._base_env(env),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_own_process_group(),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(self.workdir),
                    env=self._base_env(env),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_own_process_group(),
                )
        except OSError as e:
            return CommandResult("", f"failed to launch command: {e}", 1, False)
        # Drained into buffers rather than collected by `communicate()`, so the
        # output survives the deadline. `communicate()` returns its pair only on
        # success; cancelled at the timeout it yields nothing, and everything the
        # command printed before it wedged was discarded — which is precisely the
        # output worth having. A hanging `pytest` names the test it stopped on; a
        # hanging build names the file. Reporting "timed out after 60s" and
        # nothing else turns a diagnosable hang into a guess.
        #
        # DockerCell never had this problem: its `timeout -s KILL` fires INSIDE
        # the container, so `docker exec` returns normally, with the output. The
        # two backends disagreeing about what a timeout tells you is the kind of
        # difference that gets debugged twice.
        out_buf = Retained(self.policy.max_output_bytes)
        err_buf = Retained(self.policy.max_output_bytes)
        drain = asyncio.gather(drain_pipe(proc.stdout, out_buf, "stdout", on_output),
                               drain_pipe(proc.stderr, err_buf, "stderr", on_output))
        try:
            await self._await_drain(drain, t, on_output)
            await asyncio.wait_for(proc.wait(), timeout=REAP_GRACE_S)
        except asyncio.CancelledError:
            # The operator stopped this, and stopping it has to mean the process
            # dies — not just that we stopped waiting. The no-kill path shields
            # the readers so a passed deadline cannot cancel them, and that
            # shield would otherwise survive this cancellation too: the command
            # would keep running, holding its port and its lock, with nothing
            # left watching it. "I decide when it stops" has to include being
            # able to make it stop.
            drain.cancel()
            _kill_tree(proc)
            _release_pipes(proc)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            # `wait_for` has already cancelled the readers; the buffers keep
            # everything they had read up to that point, which is the whole
            # reason they exist.
            _kill_tree(proc)
            # Pipes released BEFORE the reap, and the order is the whole point.
            # The readers were cancelled with the transport still attached, and
            # on a cancelled transport `wait()` does not come back when the
            # process dies — it comes back when the readers do. Waiting first
            # meant every timed-out command paid the full grace below before
            # returning. Closing the transport ends them, and the reap is
            # then immediate.
            _release_pipes(proc)
            # Bounded even so: a cleanup path that can block forever is the bug
            # this is fixing wearing a different hat.
            try:
                await asyncio.wait_for(proc.wait(), timeout=REAP_GRACE_S)
            except (asyncio.TimeoutError, TimeoutError, ProcessLookupError):
                pass
            note = f"\n[command timed out after {t}s and was killed]"
            return CommandResult(
                self._cap(out_buf.text()),
                self._cap(err_buf.text()) + note,
                124,
                True,
            )
        return CommandResult(
            self._cap(out_buf.text()),
            self._cap(err_buf.text()),
            proc.returncode if proc.returncode is not None else 1,
            False,
        )

    async def write(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    async def reset(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        # The workspace persists on disk for inspection; nothing to tear down.
        return None
