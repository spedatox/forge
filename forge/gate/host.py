"""Which machine this peer speaks for.

Optimus is ONE agent that may run in several places at once — on the server and
on the owner's PC. Mark VI keys its connections by (agent_id, host) so those
coexist, but that only works if each peer says which machine it is. Until it
did, every peer registered as "default" and the second to connect silently
displaced the first: on 2026-08-06 a laptop running `forge connect` took over
the server peer's slot, and nothing anywhere reported a problem. Turns went to
the laptop; the server peer stayed up holding a socket Mark VI had forgotten.

`roots` is the other half. A peer advertises the directories it will work in so
Mark VI can route by path — the owner picks a folder and the folder identifies
the machine. Advertising nothing means "any path valid for my platform", which
is what the server peer wants and why it needs no configuration.

**Advertising is not enforcement.** These are hints for routing. What a peer
will actually do is bounded on the peer, by its own tools and permission gate —
a compromised or confused backend must not be able to talk this process into
working somewhere it was never pointed at.
"""
from __future__ import annotations

import os
import platform
import socket


def host_id() -> str:
    """A stable name for this machine.

    FORGE_HOST wins so an operator can name a box deliberately; otherwise the
    hostname, which is already unique on any network the peer can reach Mark VI
    over. Never "default" — that is the shared slot this exists to stop two
    machines fighting over.
    """
    explicit = os.environ.get("FORGE_HOST", "").strip()
    if explicit:
        return explicit
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    # Bare hostname, lowercased: "DESKTOP-TGJOLE3.local" and "desktop-tgjole3"
    # are the same machine, and a peer that reconnects under a different
    # spelling would look like a second one.
    name = name.split(".")[0].lower()
    return name or "unknown-host"


def platform_id() -> str:
    """"windows" or "linux" — how Mark VI decides whether a path suits us."""
    return "windows" if platform.system().lower().startswith("win") else "linux"


def roots() -> list[str]:
    """Directories this peer will work in, from FORGE_ROOTS (os.pathsep-separated).

    Empty means "anything well-formed for my platform", which preserves the
    server peer's long-standing behaviour without it having to declare it.
    """
    raw = os.environ.get("FORGE_ROOTS", "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(os.pathsep) if part.strip()]
