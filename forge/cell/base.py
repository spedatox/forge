"""Cell contract — the exact four-method surface from §8, plus the policy that
governs every backend that implements it."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

OnOutput = Callable[[str, str], None]
"""(stream, text) as a command produces it. "stdout" or "stderr"."""


@dataclass(frozen=True)
class CommandResult:
    """The sole return shape of Cell.run (§8)."""
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


@dataclass(frozen=True)
class CellPolicy:
    """Resource envelope for a Cell. Defaults are the safe posture (§8):
    no network, bounded CPU / memory / time, output capped."""
    allow_network: bool = False        # outbound network only if the job asks (§8, §9)
    cpus: float = 1.0                   # CPU cores (DockerCell --cpus)
    memory_mb: int = 1024              # memory ceiling (DockerCell --memory)
    pids_limit: int = 256              # fork-bomb guard (DockerCell --pids-limit)
    default_timeout_s: int = 60        # per-command wall clock when a call omits one
    max_timeout_s: int = 600           # hard ceiling a per-command timeout cannot exceed

    kill_on_timeout: bool = True
    """Whether the wall clock above ENDS a command or merely reports on it.

    True is the headless posture and the default, because a dispatched job has
    nobody watching it: a command that never returns would park the loop
    forever, and the only correct answer when no operator exists is to stop it.

    False is the interactive posture. The timeout becomes a repeating notice,
    the command keeps running, and ctrl+c is what ends it — which is what an
    operator watching a long build actually wants, because the alternative is
    losing four minutes of work to a limit they would have waived. It is only
    safe in company: it requires both live output (so the wait is informed) and
    a working interrupt (so the decision can be acted on). Setting it without
    either is how a harness hangs with a straight face."""
    max_output_bytes: int = 100_000    # cap returned stdout+stderr so a runaway can't flood
    # ── Lab posture ────────────────────────────────────────────────────────────
    # Off by default: every Cell is non-root with ALL Linux capabilities dropped.
    # An OPERATOR (never a job) can relax this per agent in the profile, exactly
    # like allow_network — a security agent needs root to provision its own
    # toolchain (apt) and raw-socket caps to scan, so its Cell IS its lab.
    env: dict = field(default_factory=dict)
    """Environment applied to every command in this Cell.

    Set from the agent's profile. Its first use is the git identity: putting
    it here rather than in a dedicated commit tool means ANY route to a
    commit is attributed correctly — including `run_command git commit`,
    which is the one an agent actually reaches for."""

    run_as_root: bool = False          # uid 0 in the container (DockerCell --user 0:0)
    cap_add: tuple[str, ...] = ()      # caps whitelisted back after --cap-drop ALL


class Cell(abc.ABC):
    """Abstract isolated sandbox. One instance == one agent's world.

    Implementations must guarantee that `run`, `read`, and `write` cannot reach
    outside the Cell's workspace, and that `run` always returns a CommandResult
    (never raises for a non-zero exit or a timeout — those are data, not
    exceptions, mirroring the errors-as-results discipline of the loop)."""

    policy: CellPolicy

    @property
    def host_path(self) -> "Path | None":
        """The workspace as the harness can see it, or None when it cannot.

        Search and navigation run Warden-side over this path — the same
        separation the Graphify sidecar already uses, and for the same reason:
        the Cell's isolation posture governs *generated code*, not the harness's
        own instruments. A backend with no host-visible workspace (an ephemeral
        container, a remote VM) returns None, and those tools report themselves
        unavailable rather than guessing. Concrete, not abstract: None is a
        correct answer, so a new backend is not required to have one."""
        return None

    # ── The active working directory (worktree isolation) ────────────────────
    # A path relative to the workspace root; empty means the workspace itself.
    # When set, it is BOTH the directory commands run in AND the escape
    # boundary read/write validate against — narrowing, never widening. That
    # second half is what makes `enter_worktree` isolation rather than a
    # convenience: inside a worktree the agent cannot write to the main
    # checkout even by spelling out `../`.
    _subpath: str = ""

    @property
    def subpath(self) -> str:
        return self._subpath

    def enter_subpath(self, rel: str) -> None:
        """Narrow the working directory to `rel` beneath the workspace."""
        self._subpath = rel.replace("\\", "/").strip("/")

    def leave_subpath(self) -> None:
        self._subpath = ""

    @abc.abstractmethod
    async def start(self) -> None:
        """Provision the sandbox (create container / workspace). Idempotent."""

    @abc.abstractmethod
    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None,
                  on_output: "OnOutput | None" = None) -> CommandResult:
        """Execute a shell command inside the Cell and return its result.

        `on_output` is called with ("stdout" | "stderr", text) as the command
        produces it, for an operator watching. It is a courtesy channel, not the
        result: the returned `CommandResult` is still the whole truth, and a
        backend that cannot stream may simply never call it. Nothing that
        depends on correctness may read from it.

        The callback is invoked from the event loop and must not block or raise.
        A raising callback is swallowed by the backend — output rendering is
        never allowed to fail a command."""

    @abc.abstractmethod
    async def write(self, path: str, content: str) -> None:
        """Write text to `path`, interpreted relative to the Cell workspace."""

    @abc.abstractmethod
    async def read(self, path: str) -> str:
        """Read text from `path`, interpreted relative to the Cell workspace."""

    @abc.abstractmethod
    async def reset(self) -> None:
        """Discard all Cell state and return to a clean workspace."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear the sandbox down. Called when the job ends."""

    def _clamp_timeout(self, timeout: int | None) -> int:
        t = self.policy.default_timeout_s if timeout is None else int(timeout)
        return max(1, min(t, self.policy.max_timeout_s))

    def _cap(self, text: str) -> str:
        """Bound one stream, keeping head AND tail.

        Head-only truncation here silently defeated the head+tail preview in
        `warden/results.py`: this cap runs first, at the Cell boundary, so on
        any command whose output exceeds `max_output_bytes` the tail was already
        gone before the layer that exists to preserve it ever saw the text. That
        is the exact case both layers name as the one that matters — a failing
        build or test run puts the answer at the END, after the noise.

        The middle is what goes, and the omission is stated where it happened so
        the model can tell "this output was cut" from "this output ended"."""
        limit = self.policy.max_output_bytes
        if len(text) <= limit:
            return text
        half = limit // 2
        omitted = len(text) - 2 * half
        return (f"{text[:half]}\n"
                f"…[{omitted} bytes omitted from the middle of this stream]…\n"
                f"{text[-half:]}")
