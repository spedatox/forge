"""DockerCell — the production Cell backend (§8).

One throwaway container per agent, matching the posture of Mark VI's own
`packages/sandbox`: isolated, resource-capped, non-root, no host mounts beyond
the workspace volume, `--network none` unless the job requests network. The
container is long-lived for the job (started once, `sleep infinity`); commands
run via `docker exec`, so filesystem and installed-package state persist across
calls the way a real machine would. `reset()` destroys and recreates it.

Rationale for Docker over a microVM/gVisor (documented in the README): it is the
same technology Mark VI already sandboxes with, it is a single well-understood
dependency, and kernel namespaces + cgroups give a real isolation boundary with
resource caps. A microVM (Firecracker) would be stronger but adds a KVM/Linux
requirement the single-operator Forge does not need for its threat model.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
import uuid
from pathlib import Path

from forge.cell.base import Cell, CellPolicy, CommandResult
from forge.cell.stream import REAP_GRACE_S as _REAP_GRACE_S, Retained as _Retained, drain as _drain

WORKDIR = "/workspace"
# The Cell's default uid — non-root. A JOB can never change this; only an
# OPERATOR, via CellPolicy.run_as_root in the agent's profile (a lab agent that
# must provision its own tooling), flips the container to uid 0. The distinction
# is the whole point: the container can't talk its way up, the operator declares
# it up front, and every other agent stays exactly as locked down as before.
CELL_UID = 1000

_EXEC_GRACE_S = 10
"""How long the local client waits past the container-side deadline.

The two timers are for different failures: the inner one kills a command that
overran, the outer one gives up on a `docker exec` that never came back at all.
Firing them together would make the outer one mask the inner, which is the
arrangement that left processes running inside the container."""


class DockerError(RuntimeError):
    pass


class DockerCell(Cell):
    def __init__(self, name_hint: str, image: str, policy: CellPolicy,
                 workspace_mount: "Path | None" = None) -> None:
        self.image = image
        self.policy = policy
        # Effective uid for this Cell: root only when the operator opted the
        # agent into a lab posture, else the locked-down non-root default.
        self.uid = 0 if policy.run_as_root else CELL_UID
        # Optional host directory bind-mounted to /workspace — the only host
        # mount, matching Mark VI's packages/sandbox posture. When None, the
        # workspace is ephemeral inside the container.
        self.workspace_mount = Path(workspace_mount).resolve() if workspace_mount else None
        # Unique per instance so two agents can never collide on one container (§9.1).
        self.container = f"forge-cell-{name_hint}-{uuid.uuid4().hex[:8]}"
        self._started = False
        self._can_timeout = False
        """Whether the image ships coreutils' `timeout`, settled once at start.

        Probed rather than assumed: the default image has it, an arbitrary one
        the operator points at may not, and wrapping every command in a binary
        that is not there would turn one missing convenience into every command
        failing with `sh: timeout: not found`."""

    @property
    def host_path(self) -> "Path | None":
        """The bind mount, when there is one. An ephemeral in-container
        workspace has no host path and search reports itself unavailable."""
        if self.workspace_mount is not None and self._subpath:
            return self.workspace_mount / self._subpath
        return self.workspace_mount

    async def _docker(self, *args: str, timeout: int | None = None,
                      stdin: bytes | None = None,
                      on_output=None) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if stdin is not None:
            # `write` pushes file content down stdin, and the streaming path
            # does not carry a writer. Nothing that takes stdin is a command an
            # operator watches, so there is nothing to stream for it either.
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(input=stdin), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
                await proc.wait()
                return 124, b"", b"docker call timed out"
            return proc.returncode if proc.returncode is not None else 1, out, err

        # Streamed, and for the same reason SubprocessCell is: output collected
        # by `communicate()` is lost when the deadline cancels it, and the whole
        # point of watching a build is seeing it before it ends.
        out_buf = _Retained(self.policy.max_output_bytes)
        err_buf = _Retained(self.policy.max_output_bytes)
        drain = asyncio.gather(_drain(proc.stdout, out_buf, "stdout", on_output),
                               _drain(proc.stderr, err_buf, "stderr", on_output))
        try:
            if timeout is None:
                await drain
            else:
                await asyncio.wait_for(drain, timeout=timeout)
            await asyncio.wait_for(proc.wait(), timeout=_REAP_GRACE_S)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_REAP_GRACE_S)
            except (asyncio.TimeoutError, TimeoutError, ProcessLookupError):
                pass
            return 124, out_buf.raw(), err_buf.raw() + b"\ndocker call timed out"
        code = proc.returncode if proc.returncode is not None else 1
        return code, out_buf.raw(), err_buf.raw()

    async def start(self) -> None:
        if self._started:
            return
        args = [
            "run", "-d", "--name", self.container,
            "--workdir", WORKDIR,
            "--memory", f"{self.policy.memory_mb}m",
            "--cpus", str(self.policy.cpus),
            "--pids-limit", str(self.policy.pids_limit),
            "--user", f"{self.uid}:{self.uid}",
            # Whitelist model: drop everything, then add back only the caps the
            # operator granted this agent (empty for every non-lab agent, so the
            # default posture is unchanged — ALL dropped).
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
        ]
        for cap in self.policy.cap_add:
            args += ["--cap-add", cap]
        if not self.policy.allow_network:
            args += ["--network", "none"]       # default posture: no outbound network (§8)
        if self.workspace_mount is not None:
            self.workspace_mount.mkdir(parents=True, exist_ok=True)
            self._hand_mount_to_cell_user()
            args += ["--mount", f"type=bind,src={self.workspace_mount},dst={WORKDIR}"]
        args += [self.image, "sleep", "infinity"]
        code, _out, err = await self._docker(*args, timeout=60)
        if code != 0:
            raise DockerError(f"could not start Cell container: {err.decode('utf-8', 'replace')}")
        # Ensure the workspace exists and is writable by the non-root user. On a
        # bind mount this can only fix an ephemeral (in-image) workdir: the
        # container drops ALL capabilities, so even uid 0 has no CAP_CHOWN and
        # this call cannot touch host-owned files. _hand_mount_to_cell_user did
        # that part from outside, where the privilege actually exists.
        await self._docker("exec", "--user", "0:0", self.container,
                           "sh", "-c",
                           f"mkdir -p {WORKDIR} && chown {self.uid}:{self.uid} {WORKDIR} 2>/dev/null || true",
                           timeout=15)
        # Fail loud rather than hand the Warden a Cell whose every write dies
        # with EACCES halfway through a job (§9.5: assume the environment).
        code, _o, _e = await self._docker(
            "exec", self.container, "sh", "-c", f"test -w {WORKDIR}", timeout=15)
        if code != 0:
            await self.close()
            raise DockerError(
                f"Cell workspace {WORKDIR} is not writable by uid {self.uid}. "
                f"Give {self.workspace_mount} to that uid, or make it group-writable "
                f"for a group the uid belongs to."
            )
        # One probe, so `run` never has to guess. A failure here means "no
        # container-side deadline", which is the behaviour this Cell had all
        # along — not a reason to refuse to start.
        probe, _o, _e = await self._docker(
            "exec", self.container, "sh", "-c", "command -v timeout", timeout=15)
        self._can_timeout = probe == 0
        self._started = True

    def _hand_mount_to_cell_user(self) -> None:
        """Give the bind mount to the Cell's uid, from outside the container.

        This has to happen here because it cannot happen in there: the Cell
        drops every capability, so it holds no CAP_CHOWN even as uid 0 and a
        root-owned bind mount stays unwritable no matter what the container
        does to it. Best-effort by design — when the Forge is unprivileged this
        is a no-op, and the writability probe in start() is what turns a
        still-unusable workspace into a loud error instead of a job that fails
        on its first write.

        The group is deliberately left alone. On the deployed host it is the
        vault's group, and that is what lets the file desktop manage the output
        of a job it did not run.
        """
        chown = getattr(os, "chown", None)       # POSIX only; absent on Windows
        if chown is None or self.workspace_mount is None:
            return
        try:
            st = self.workspace_mount.stat()
            if st.st_uid != self.uid:
                chown(self.workspace_mount, self.uid, -1)
            # Keep the group's access at least as wide as the owner's, so a
            # shared vault group can still read and clean up what lands here.
            os.chmod(self.workspace_mount, st.st_mode | 0o070)
        except OSError:
            return                               # unprivileged: start() will report it

    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None,
                  on_output=None) -> CommandResult:
        if not self._started:
            await self.start()
        t = self._clamp_timeout(timeout)
        exec_args = ["exec"]
        merged = {**(self.policy.env or {}), **(env or {})}
        for k, v in merged.items():
            exec_args += ["--env", f"{k}={v}"]
        if self._subpath:
            command = f"cd {shlex.quote(self._workdir)} && {command}"
        # Bounded INSIDE the container as well as outside. `wait_for` below only
        # reaches the local `docker exec` client — killing it detaches from a
        # process that carries on running in the container, holding the port it
        # bound and eating the Cell's whole cpu/memory allowance for the rest of
        # the job. The next command then times out too, and the one after that,
        # which is what "Forge got stuck running a command" actually looks like
        # from outside. Only when the image has `timeout`: a container without
        # coreutils would answer `sh: timeout: not found` to everything, and a
        # missing convenience must never break every command.
        # `kill_on_timeout` decides whether the container-side kill is armed at
        # all. Cleared, the operator is the stopping condition and arming it
        # here would override them from inside the container, where ctrl+c
        # cannot reach — the one place the decision must NOT be made.
        if self._can_timeout and self.policy.kill_on_timeout:
            command = f"timeout -s KILL {t} sh -c {shlex.quote(command)}"
        exec_args += [self.container, "sh", "-c", command]
        # The outer deadline is deliberately slack: the container-side kill is
        # the one that should fire, and this is the backstop for a `docker exec`
        # that hangs in the daemon rather than in the command. Without a kill
        # policy there is no outer deadline either; the command runs until it
        # ends or the operator stops it.
        outer = t + _EXEC_GRACE_S if self.policy.kill_on_timeout else None
        code, out, err = await self._docker(*exec_args, timeout=outer,
                                            on_output=on_output)
        timed_out = code == 124
        return CommandResult(
            self._cap(out.decode("utf-8", "replace")),
            self._cap(err.decode("utf-8", "replace")),
            code,
            timed_out,
        )

    @property
    def _workdir(self) -> str:
        """The in-container working directory: /workspace, or the
        subdirectory an active worktree narrowed it to."""
        return f"{WORKDIR}/{self._subpath}" if self._subpath else WORKDIR

    def _guard(self, path: str) -> str:
        # Resolve inside the ACTIVE workdir and refuse traversal outside it —
        # inside a worktree the boundary tightens with it.
        root = self._workdir
        p = path if path.startswith("/") else f"{root}/{path}"
        norm = str(Path(p).as_posix())
        if not (norm == root or norm.startswith(root + "/")):
            raise PermissionError(f"path escapes the Cell workspace: {path!r}")
        return norm

    async def write(self, path: str, content: str) -> None:
        target = self._guard(path)
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = str(Path(target).parent.as_posix())
        cmd = f"mkdir -p {shlex.quote(parent)} && echo {b64} | base64 -d > {shlex.quote(target)}"
        code, _out, err = await self._docker("exec", self.container, "sh", "-c", cmd, timeout=30)
        if code != 0:
            raise DockerError(f"Cell write failed: {err.decode('utf-8', 'replace')}")

    async def read(self, path: str) -> str:
        target = self._guard(path)
        code, out, err = await self._docker("exec", self.container, "cat", target, timeout=30)
        if code != 0:
            raise FileNotFoundError(err.decode("utf-8", "replace") or f"cannot read {path!r}")
        return out.decode("utf-8", "replace")

    async def reset(self) -> None:
        await self.close()
        self._started = False
        await self.start()

    async def close(self) -> None:
        await self._docker("rm", "-f", self.container, timeout=30)
        self._started = False

    @staticmethod
    async def available() -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return await asyncio.wait_for(proc.wait(), timeout=8) == 0
        except (OSError, asyncio.TimeoutError):
            return False
