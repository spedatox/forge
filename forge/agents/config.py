"""AgentConfig — the injected identity the Warden is parameterized with (§2).

Structurally no different from Mark VI's fork contract: the engine is untouched by
identity; identity is data loaded from the agent's folder.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CellSpec:
    allow_network: bool = False     # default posture: no outbound network (§8)
    cpus: float = 1.0
    memory_mb: int = 1024
    timeout_s: int = 60
    backend: str | None = None      # None → use the process-wide FORGE_CELL_BACKEND
    image: str | None = None        # None → use the process-wide FORGE_CELL_IMAGE.
    #                                 A security agent points this at a toolchain
    #                                 image (nmap/nikto/…) so its Cell IS its lab.
    # ── Lab posture (operator-set, mirrors CellPolicy) ──────────────────────────
    # Off by default — every agent's Cell is non-root with ALL caps dropped. A
    # security agent that must provision its own tooling and run raw-socket scans
    # opts in here; nothing a job says can reach these.
    run_as_root: bool = False       # container uid 0 (apt, privileged tooling)
    cap_add: tuple[str, ...] = ()   # Linux caps whitelisted back (e.g. NET_RAW)


@dataclass(frozen=True)
class GitIdentity:
    """Who git records as the author of work this agent does.

    An agent that commits under the operator's name makes the history lie
    about who wrote the code — which matters most later, when someone is
    trying to work out why a change was made and asks the wrong person.

    No account anywhere is required: git only stores a name and an address.
    A host like GitHub will show the name plainly and link it to a profile
    only if the address belongs to a registered account, so an account buys
    an avatar and a profile link and nothing else."""
    name: str = ""
    email: str = ""

    def env(self) -> dict[str, str]:
        """Author AND committer. Setting only the author leaves the operator
        recorded as committer, which reads as them having applied a patch
        they never saw."""
        if not (self.name and self.email):
            return {}
        return {"GIT_AUTHOR_NAME": self.name, "GIT_AUTHOR_EMAIL": self.email,
                "GIT_COMMITTER_NAME": self.name, "GIT_COMMITTER_EMAIL": self.email}


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str                   # the discriminator; unique, stable (§2, mirrors Mark VI)
    name: str
    domain: str
    model_ref: str                  # model IDs live in the profile, never in core (Rule 10)
    tool_names: tuple[str, ...]     # the tool allowlist — the security boundary (§2/§4)
    system_prompt: str
    permission_mode: str = "act"    # "act" | "plan" (§6)
    max_iterations: int = 200       # runaway guard, not a work limit (§3)
    cell: CellSpec = CellSpec()
    git: GitIdentity = GitIdentity()
