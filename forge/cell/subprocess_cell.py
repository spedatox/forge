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
from pathlib import Path

from forge.cell.base import Cell, CellPolicy, CommandResult


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

    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None) -> CommandResult:
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
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(self.workdir),
                    env=self._base_env(env),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except OSError as e:
            return CommandResult("", f"failed to launch command: {e}", 1, False)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=t)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return CommandResult("", f"command timed out after {t}s", 124, True)
        return CommandResult(
            self._cap(out.decode("utf-8", "replace")),
            self._cap(err.decode("utf-8", "replace")),
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
