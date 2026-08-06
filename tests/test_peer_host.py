"""A peer has to say which machine it is.

The regression, 2026-08-06. Mark VI keys connections by (agent_id, host) so one
agent can run in several places at once. Forge never sent a host, so every peer
registered as "default" — and when the owner ran `forge connect` on their
laptop it dropped into the same slot as the server peer and displaced it.

Nothing failed. Both processes stayed up, both logged `peer_registered`, and
the laptop quietly began receiving every Optimus turn while the server peer sat
holding a socket Mark VI had forgotten. The only visible symptom was a terminal
that looked like it had hung.
"""
from __future__ import annotations

import os

import pytest

from forge.gate import host


# ── The host name ────────────────────────────────────────────────────────────


def test_a_peer_never_registers_as_default(monkeypatch):
    """"default" is the shared slot. Landing in it is the bug."""
    monkeypatch.delenv("FORGE_HOST", raising=False)
    assert host.host_id() != "default"


def test_an_operator_can_name_the_machine(monkeypatch):
    monkeypatch.setenv("FORGE_HOST", "arel-pc")
    assert host.host_id() == "arel-pc"


def test_the_hostname_is_used_when_nothing_is_declared(monkeypatch):
    monkeypatch.delenv("FORGE_HOST", raising=False)
    monkeypatch.setattr(host.socket, "gethostname", lambda: "DESKTOP-TGJOLE3")
    assert host.host_id() == "desktop-tgjole3"


def test_the_name_is_stable_across_spellings(monkeypatch):
    """A peer reconnecting under a different spelling of its own name would
    register as a second machine and leak a slot on every reconnect."""
    monkeypatch.delenv("FORGE_HOST", raising=False)
    monkeypatch.setattr(host.socket, "gethostname", lambda: "DESKTOP-TGJOLE3.local")
    first = host.host_id()
    monkeypatch.setattr(host.socket, "gethostname", lambda: "desktop-tgjole3")
    assert host.host_id() == first


def test_a_machine_with_no_resolvable_name_still_gets_one(monkeypatch):
    monkeypatch.delenv("FORGE_HOST", raising=False)
    monkeypatch.setattr(host.socket, "gethostname", lambda: "")
    assert host.host_id() == "unknown-host"


def test_a_hostname_lookup_failure_is_not_fatal(monkeypatch):
    def _boom():
        raise OSError("no resolver")

    monkeypatch.delenv("FORGE_HOST", raising=False)
    monkeypatch.setattr(host.socket, "gethostname", _boom)
    assert host.host_id() == "unknown-host"


# ── The platform ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("system,expected", [
    ("Windows", "windows"), ("Linux", "linux"), ("Darwin", "linux"),
])
def test_the_platform_decides_which_paths_suit_us(system, expected, monkeypatch):
    """Mark VI refuses a path malformed for the peer it is aimed at, so this is
    what stops `C:\\Users\\...` reaching a Linux peer."""
    monkeypatch.setattr(host.platform, "system", lambda: system)
    assert host.platform_id() == expected


# ── Advertised roots ─────────────────────────────────────────────────────────


def test_no_roots_means_any_path_for_this_platform(monkeypatch):
    """Which is the server peer's long-standing behaviour, preserved without
    it having to declare anything."""
    monkeypatch.delenv("FORGE_ROOTS", raising=False)
    assert host.roots() == []


def test_roots_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FORGE_ROOTS", os.pathsep.join([r"C:\repos", r"D:\work"]))
    assert host.roots() == [r"C:\repos", r"D:\work"]


def test_blank_entries_are_dropped(monkeypatch):
    """A trailing separator is the normal way to write one of these by hand,
    and an empty root would advertise a claim on everything."""
    monkeypatch.setenv("FORGE_ROOTS", r"C:\repos" + os.pathsep + os.pathsep + "  ")
    assert host.roots() == [r"C:\repos"]


# ── The handshake actually carries them ──────────────────────────────────────


def test_the_registration_frame_identifies_the_machine(monkeypatch):
    """The whole point: without these three fields Mark VI cannot tell two
    peers of the same agent apart."""
    import asyncio

    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeSettings
    from forge.gate.peer import ForgePeer

    monkeypatch.setenv("FORGE_HOST", "arel-pc")
    monkeypatch.setenv("FORGE_ROOTS", r"C:\repos")
    monkeypatch.setattr(host.platform, "system", lambda: "Windows")

    registry = AgentRegistry.load()
    peer = ForgePeer(registry.get("optimus"), ForgeSettings.from_env(), registry)

    sent: list[dict] = []

    async def _capture(frame):
        sent.append(frame)

    peer._send = _capture                                      # noqa: SLF001
    asyncio.run(peer._register())                              # noqa: SLF001

    frame = sent[0]
    assert frame["type"] == "agent_register"
    assert frame["agent_id"] == "optimus"      # ONE agent...
    assert frame["host"] == "arel-pc"          # ...on a named machine
    assert frame["platform"] == "windows"
    assert frame["roots"] == [r"C:\repos"]
