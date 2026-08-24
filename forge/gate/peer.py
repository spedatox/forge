"""Mark VI peer (§7, §9.2).

Connects to Mark VI's agents WebSocket as the agent it was launched as and speaks
the backend's existing protocol:

    → agent_register (agent_id, capabilities, model_preference, host/platform/roots)
    → heartbeat (periodic)
    → memory_request {skill: record_observation, ...}  → queued facts, flushed on connect
    ← task_dispatch {task_id, from, task, cwd}     → run one job → task_result
    ← chat_request  {chat_id, history, cwd, ...}   → run one job → chat_event stream
    ← chat_cancel   {chat_id}                       → abort that run
    ← owner_memory_sync {block}                     → refresh the local memory snapshot
    ← shutdown                                      → stop, no reconnect

The peer carries no identity of its own — `cfg` is whichever AgentConfig it was
started with, so the same class serves Optimus, Centurion, or a third agent
(§2). Graceful fallback is Mark VI's side of the contract (§9.2): when this peer
is offline, Mark VI answers with its in-process profile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from forge.agents import owner_memory
from forge.agents.config import AgentConfig
from forge.gate import host
from forge.config import ForgeSettings
from forge.gate.protocol import (JobEvent, job_event_to_chat_event,
                                 job_from_chat_request, job_from_task_dispatch)
from forge.extensions import load_extensions
from forge.warden.memory import MemoryReply, RemoteMemory
from forge.warden.oracle import Answer, ChannelOracle
from forge.warden.permissions import AllowList
from forge.gate.runner import run_job
from forge.gate.cellpool import CellPool
from forge.warden.state import StopReason

logger = logging.getLogger("forge.gate.peer")

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0
_HEARTBEAT_S = 30.0
# A chat that emits no frame for this long is logged as stalled. Purely
# diagnostic — Mark VI's ExternalAgentProxy times the stream out at 300s; this
# fires first so the peer's own logs show WHICH chat went quiet and when (the
# visibility this class of hang otherwise lacks). It never aborts the run.
_CHAT_SILENCE_LOG_S = 250.0
# How often the keepalive checks a quiet stream, and the silence past which it
# nudges Mark VI's proxy so a legitimately long single tool call (a cold `wpscan`
# database update, a wide `nuclei` sweep) is not mistaken for a dead turn. It
# lives WELL under the proxy's 300s idle ceiling: real work resets the clock via
# `emit`, and when nothing real flows this backfills at ~90s cadence so the gap
# Mark VI ever sees stays near 2×90s — comfortably inside 300s with margin for
# jitter. The precise bounds that catch a GENUINE hang (per-command Cell timeout,
# the tool backstop, the iteration ceiling) all sit BELOW this, so keeping the
# socket alive here cannot mask a wedged run — it only stops the blunt 300s
# stream timeout from firing on work that is fine.
_CHAT_KEEPALIVE_S = 90.0
# chat_event types Mark VI's ExternalAgentProxy understands (its _EVENT_MAP).
_CHAT_FORWARD = frozenset({"chunk", "tool", "tool_result", "done", "error",
                           # A delegation's own activity, on its own channel so
                           # a client can show it in a panel of its own rather
                           # than mixed into the answer (forge/warden/subagents).
                           "subagent"})


class ForgePeer:
    def __init__(self, cfg: AgentConfig, settings: ForgeSettings,
                 registry: "Any") -> None:
        self.cfg = cfg
        self.settings = settings
        self.registry = registry
        # Process-wide extension layer (law 2: assembled once, at the entry
        # point, never via import side effects). Loaded here rather than per job
        # so an MCP server is started once and shared, not respawned per task.
        self.extensions = load_extensions()
        # The operator's standing approvals, shared across every job this peer
        # runs. Loaded once: "don't ask me again" that expired with the job
        # would not mean what anyone reads it as.
        self.allowlist = AllowList.load(settings.allowlist_path)
        # One oracle for the peer — the socket is the channel, so parked asks
        # from any job resolve through the same frame handler.
        self._oracle = ChannelOracle(self._send, timeout_s=settings.ask_timeout_s)
        # The owner's memory, over the same socket. One channel for the peer for
        # the same reason as the oracle: replies from any job correlate through
        # one frame handler, and a second connection to the same backend would
        # be another thing to authenticate and another thing to reconnect.
        self._memory = RemoteMemory(self._send)
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        # One live Cell per conversation, kept across its turns so a long turn's
        # tooling and caches survive a timeout+retry instead of being rebuilt
        # from the base image every time. Isolation between conversations is
        # preserved; see forge/gate/cellpool.py. Survives reconnects (the
        # containers are process-local) and is torn down when the peer stops.
        self._cellpool = CellPool()
        self._chats: dict[str, asyncio.Event] = {}     # chat_id/task_id → abort signal
        self._work: set[asyncio.Task] = set()
        self._shutdown = False
        self._stop = asyncio.Event()

    # ── Connection lifecycle ─────────────────────────────────────────────────
    async def run_forever(self) -> None:
        backoff = _BACKOFF_START_S
        while not self._shutdown and not self._stop.is_set():
            try:
                await self._serve_one()
                backoff = _BACKOFF_START_S
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("peer_connection_lost", extra={"error": f"{type(e).__name__}: {e}"})
            if self._shutdown or self._stop.is_set():
                break
            logger.info("peer_reconnect", extra={"in_s": backoff})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    def request_stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        """Reclaim process-local resources when the peer stops for good. The
        conversation Cells outlive individual turns and reconnects on purpose,
        so nothing else frees them — without this, stopping the peer leaks a
        container per live conversation."""
        await self._cellpool.close_all()

    async def _serve_one(self) -> None:
        import websockets
        headers = {"X-API-Key": self.settings.speda_api_key}
        # websockets ≥13 renamed extra_headers → additional_headers.
        try:
            major = int(websockets.__version__.split(".")[0])
        except (AttributeError, ValueError):
            major = 12
        kw = "additional_headers" if major >= 13 else "extra_headers"
        async with websockets.connect(self.settings.speda_ws_url, **{kw: headers}) as ws:
            self._ws = ws
            try:
                await self._register()
                logger.info("peer_registered",
                            extra={"agent": self.cfg.agent_id, "url": self.settings.speda_ws_url})
                # Whatever this peer queued locally while nobody was listening
                # (remember_about_owner, offline) goes out now — reconnecting
                # is the only signal this module has that Mark VI can hear it
                # again, so it does not wait for a heartbeat or a job to carry
                # it incidentally.
                self._spawn(self._flush_pending_observations())
                hb = asyncio.create_task(self._heartbeat())
                # The receive loop parks on ws.recv() and only returns when the
                # socket closes, so waiting on it alone leaves a stop request
                # unobserved for as long as the connection stays healthy —
                # under systemd that means SIGTERM is ignored until SIGKILL.
                # Race the two instead: whichever lands first ends the session.
                recv = asyncio.create_task(self._receive_loop())
                stop = asyncio.create_task(self._stop.wait())
                try:
                    done, _ = await asyncio.wait(
                        {recv, stop}, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Re-raise a connection failure so run_forever's backoff sees
                    # it; a stop request is not an error and must not reconnect.
                    if recv in done:
                        recv.result()
                finally:
                    hb.cancel()
                    recv.cancel()
                    stop.cancel()
                    await asyncio.gather(hb, recv, stop, return_exceptions=True)
            finally:
                self._ws = None
                # Losing the socket is not a reason to hang, and it is certainly
                # not a reason to proceed: every question still waiting for an
                # answer resolves to denied.
                self._oracle.abandon_all("the operator channel closed")
                # And every memory call. A write parked on a socket that has
                # gone must come back as a failure, not hang and not quietly
                # succeed — an agent that believes it filed something and did
                # not has lost the fact and the knowledge that it lost it.
                self._memory.abandon_all("the connection to Mark VI closed "
                                         "before this could be saved")
                for ev in self._chats.values():
                    ev.set()

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("peer socket not connected")
        async with self._send_lock:
            await ws.send(json.dumps(frame))

    async def _register(self) -> None:
        await self._send({
            "type": "agent_register",
            "agent_id": self.cfg.agent_id,
            "agent_name": self.cfg.name,
            "domain": self.cfg.domain,
            "capabilities": list(self.cfg.tool_names),
            "status": "online",
            "model_preference": self.cfg.model_ref,
            # Which MACHINE this peer speaks for. Mark VI keys connections by
            # (agent_id, host), so without these two peers of the same agent
            # share one slot and the second to connect silently evicts the
            # first — see forge/gate/host.py for the day that happened.
            "host": host.host_id(),
            "platform": host.platform_id(),
            "roots": host.roots(),
        })
        logger.info(
            "peer_registering",
            extra={"agent": self.cfg.agent_id, "host": host.host_id(),
                   "platform": host.platform_id(), "roots": host.roots()},
        )

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_S)
            try:
                await self._send({"type": "heartbeat", "agent_id": self.cfg.agent_id, "payload": {}})
            except Exception:  # noqa: BLE001 — the receive loop owns the disconnect
                return

    async def _flush_pending_observations(self) -> None:
        """Replay whatever remember_about_owner queued while disconnected.

        Best-effort like everything else touching this file: a flush that
        fails leaves the queue exactly as it was (owner_memory.flush_pending
        only drops a line once Mark VI actually ran it), so the next
        reconnect tries again. Never lets a bad flush take the connection
        down with it.
        """
        try:
            sent = await owner_memory.flush_pending(self._memory)
        except Exception as e:  # noqa: BLE001 — a flush failure is not a peer failure
            logger.debug("owner_memory_flush_error", extra={"error": repr(e)})
            return
        if sent:
            logger.info("owner_memory_flushed", extra={"sent": sent})

    async def _receive_loop(self) -> None:
        async for raw in self._ws:
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue
            self._dispatch(frame)
            if self._shutdown:
                return

    def _dispatch(self, frame: dict[str, Any]) -> None:
        ftype = frame.get("type")
        if ftype == "task_dispatch":
            self._spawn(self._handle_task(frame))
        elif ftype == "chat_request":
            self._spawn(self._handle_chat(frame))
        elif ftype == "chat_cancel":
            ev = self._chats.get(str(frame.get("chat_id", "")))
            if ev is not None:
                ev.set()
        elif ftype == "permission_response":
            # Additive frame (law 3). An answer to a question that already timed
            # out is dropped, not an error — a slow operator is not a bug.
            ask_id = str(frame.get("ask_id", ""))
            answer = Answer(approved=bool(frame.get("approved")),
                            remember=bool(frame.get("remember")),
                            note=str(frame.get("note", "")))
            if not self._oracle.resolve(ask_id, answer):
                logger.info("permission_response_unmatched", extra={"ask_id": ask_id})
        elif ftype == "question_response":
            # The open-question counterpart of permission_response. Separate
            # frame because the payload is prose rather than a verdict, and a
            # consumer rendering Allow/Deny buttons would have nowhere to put it.
            ask_id = str(frame.get("ask_id", ""))
            if not self._oracle.answer(ask_id, str(frame.get("text", ""))):
                logger.info("question_response_unmatched", extra={"ask_id": ask_id})
        elif ftype == "owner_memory_sync":
            # Additive frame (law 3), unsolicited: Orion pushes this once a
            # night, unprompted, after recomposing owner.md/current.md (see
            # app/skills/forge_sync.py on Mark VI's side). No request_id to
            # correlate — this refreshes the same on-disk snapshot the live
            # per-turn `memory_block` already writes, so the offline TUI is
            # never more than one audit cycle behind even on a night this
            # peer ran no connected job at all.
            owner_memory.remember(str(frame.get("block", "")))
        elif ftype == "memory_response":
            # Mark VI's answer to one memory command. `ok` is carried explicitly
            # rather than inferred from the presence of text: a `delete` that
            # succeeds says very little, and guessing from an empty body would
            # report the one operation with no output as a failure.
            request_id = str(frame.get("request_id", ""))
            reply = MemoryReply(ok=bool(frame.get("ok", True)),
                                text=str(frame.get("result", "")))
            if not self._memory.resolve(request_id, reply):
                logger.info("memory_response_unmatched", extra={"request_id": request_id})
        elif ftype == "shutdown":
            logger.info("peer_shutdown_requested")
            self._shutdown = True
        # acknowledge / anything else: ignore

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._work.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Retrieve a finished handler's outcome. Without this an exception in a
        handler is never retrieved and vanishes unlogged — exactly how a chat
        died mid-turn while Mark VI waited out its 300s idle timeout with no
        terminal frame and no trace of why."""
        self._work.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("peer_task_crashed", exc_info=exc)

    async def _safe_terminal_error(self, chat_id: str, detail: str) -> None:
        """Best-effort terminal error frame so Mark VI never waits out its idle
        timeout on a crashed run. Guarded: if the socket is already gone, Mark
        VI's fail_agent path delivers the error instead, so a failure here is
        nothing to escalate."""
        try:
            await self._send({
                "type": "chat_event", "agent_id": self.cfg.agent_id,
                "chat_id": chat_id,
                "event": {"type": "error",
                          "data": f"{self.cfg.name} hit an error mid-task "
                                  f"({detail})."},
            })
        except Exception:  # noqa: BLE001 — the socket is gone; nothing to do
            pass

    async def _chat_watchdog(self, chat_id: str, last_activity) -> None:
        """Log a chat that has gone silent past the ceiling. Diagnostic only: it
        never aborts the run — a legitimate long scan is not a bug — but it turns
        a silent stall into a dated log line naming the chat, so the next one is
        traceable instead of invisible."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_CHAT_SILENCE_LOG_S)
            quiet = loop.time() - last_activity()
            if quiet >= _CHAT_SILENCE_LOG_S:
                logger.warning("chat_stream_silent",
                               extra={"chat_id": chat_id,
                                      "quiet_s": round(quiet, 1)})

    async def _chat_keepalive(self, chat_id: str, last_activity) -> None:
        """Keep a legitimately-busy stream alive past Mark VI's 300s idle ceiling.

        The proxy's clock resets only when a `chat_event` reaches it, and the run
        loop produces one only when the model streams text or a tool starts or
        returns. A single long command (a cold `wpscan` DB update, a wide
        `nuclei` sweep) makes none of those for its whole duration, so the stream
        goes silent and Mark VI kills a turn that is working fine — the exact
        "stopped responding mid-task" the operator sees.

        This backfills a frame while, and only while, real activity is absent:
        `last_activity` tracks genuine events (never the keepalive itself), so a
        stream that is actually streaming is never touched, and `_chat_watchdog`
        still logs true silence for diagnostics. The nudge is an EMPTY `chunk` —
        a type the proxy already maps and resets on, whose empty payload appends
        nothing to the answer, so it is invisible to the client."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_CHAT_KEEPALIVE_S)
            if loop.time() - last_activity() < _CHAT_KEEPALIVE_S:
                continue  # real frames are flowing — no nudge needed
            try:
                await self._send({
                    "type": "chat_event", "agent_id": self.cfg.agent_id,
                    "chat_id": chat_id, "event": {"type": "chunk", "data": ""}})
            except Exception:  # noqa: BLE001 — socket gone; the run will unwind
                return

    # ── Handlers ─────────────────────────────────────────────────────────────
    async def _handle_task(self, frame: dict[str, Any]) -> None:
        """task_dispatch → run one job, answer with a single task_result."""
        request = job_from_task_dispatch(frame, self.cfg.agent_id)
        signal = asyncio.Event()
        self._chats[request.job_id] = signal

        async def sink(_ev: JobEvent) -> None:      # fire-and-await: no streaming
            return None

        try:
            term = await run_job(request, settings=self.settings, registry=self.registry,
                                 emit=sink, signal=signal,
                                 tool_providers=self.extensions.tool_providers(),
                                 fragments=self.extensions.fragments,
                                 hooks=self.extensions.hooks,
                                 bus=self.extensions.bus,
                                 oracle=self._oracle, allowlist=self.allowlist,
                                 memory=self._memory)
            status = "ok" if term.reason is StopReason.COMPLETED else "error"
            result = term.final_text or (term.error or "(no output)")
            await self._send({
                "type": "task_result", "agent_id": self.cfg.agent_id,
                "task_id": request.job_id, "result": result, "status": status,
            })
        except Exception as e:  # noqa: BLE001 — a crashed job must still answer
            # A dispatch caller blocks on exactly one task_result; if run_job
            # raises and we stay silent, that caller hangs until ITS timeout.
            # Answer with an error result and log the trace we would have lost.
            logger.exception("task_job_crashed", extra={"task_id": request.job_id})
            try:
                await self._send({
                    "type": "task_result", "agent_id": self.cfg.agent_id,
                    "task_id": request.job_id,
                    "result": f"{self.cfg.name} hit an error: {type(e).__name__}: {e}",
                    "status": "error",
                })
            except Exception:  # noqa: BLE001 — socket gone; caller times out
                pass
        finally:
            self._chats.pop(request.job_id, None)

    async def _handle_chat(self, frame: dict[str, Any]) -> None:
        """chat_request → run one job, stream chat_event frames until terminal."""
        request = job_from_chat_request(frame, self.cfg.agent_id)
        chat_id = str(frame.get("chat_id", request.job_id))
        signal = asyncio.Event()
        self._chats[chat_id] = signal
        terminal_seen = False
        last_activity = asyncio.get_running_loop().time()

        async def emit(ev: JobEvent) -> None:
            nonlocal terminal_seen, last_activity
            if ev.type not in _CHAT_FORWARD:
                return
            if ev.type in ("done", "error"):
                terminal_seen = True
            await self._send({"type": "chat_event", "agent_id": self.cfg.agent_id,
                              "chat_id": chat_id, "event": job_event_to_chat_event(ev)})
            # Reset the silence clock only on a frame that actually REACHED Mark
            # VI — its idle counter resets on nothing else. Timestamping every
            # JobEvent (including the internal ones this filter drops) would read
            # a burst of non-forwarded events as activity while Mark VI received
            # nothing and timed out anyway, leaving the keepalive silent through
            # the exact stall it exists to cover.
            last_activity = asyncio.get_running_loop().time()

        watchdog = asyncio.create_task(
            self._chat_watchdog(chat_id, lambda: last_activity))
        keepalive = asyncio.create_task(
            self._chat_keepalive(chat_id, lambda: last_activity))
        try:
            term = await run_job(request, settings=self.settings, registry=self.registry,
                                 emit=emit, signal=signal,
                                 tool_providers=self.extensions.tool_providers(),
                                 fragments=self.extensions.fragments,
                                 hooks=self.extensions.hooks,
                                 bus=self.extensions.bus,
                                 oracle=self._oracle, allowlist=self.allowlist,
                                 # Labelled with the chat so Mark VI knows whose
                                 # turn is writing; the peer runs several
                                 # conversations over one socket and the frame is
                                 # the only place that can carry the association.
                                 memory=self._memory.scoped(chat_id),
                                 # Reuse this conversation's Cell across its turns
                                 # (keyed on chat_id inside run_job via job_id) so
                                 # a timeout+retry keeps the tools and caches the
                                 # turn already installed. Dispatch stays throwaway.
                                 cell_pool=self._cellpool)
            if not terminal_seen:
                # Ensure Mark VI always gets a terminal frame (abort path, etc.).
                final_type = "done" if term.reason is StopReason.COMPLETED else "error"
                data = term.final_text if final_type == "done" else (term.error or "run ended")
                await self._send({"type": "chat_event", "agent_id": self.cfg.agent_id,
                                  "chat_id": chat_id, "event": {"type": final_type, "data": data}})
        except Exception as e:  # noqa: BLE001 — a crashed run must still terminate
            # This is the gap that let a chat die mid-turn in silence: run_job
            # raised, control skipped straight past the terminal-frame emit to
            # `finally`, and Mark VI waited out its full 300s idle timeout with
            # no error and no trace. Emit the terminal frame and keep the trace.
            logger.exception("chat_job_crashed", extra={"chat_id": chat_id})
            if not terminal_seen:
                await self._safe_terminal_error(chat_id, f"{type(e).__name__}: {e}")
        finally:
            watchdog.cancel()
            keepalive.cancel()
            self._chats.pop(chat_id, None)


def main() -> int:
    """`python -m forge.gate.peer` — connect as the agent named in FORGE_AGENT."""
    import os
    import signal as signalmod
    from forge.agents.registry import AgentRegistry

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = ForgeSettings.from_env()
    if not settings.speda_api_key:
        raise SystemExit("SPEDA_API_KEY is required — the peer authenticates the WS handshake with it.")
    registry = AgentRegistry.load()
    agent_id = os.environ.get("FORGE_AGENT", "optimus")
    cfg = registry.get(agent_id)
    peer = ForgePeer(cfg, settings, registry)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signalmod.SIGINT, signalmod.SIGTERM):
            try:
                loop.add_signal_handler(sig, peer.request_stop)
            except (NotImplementedError, OSError):
                signalmod.signal(sig, lambda *_: peer.request_stop())
        try:
            await peer.run_forever()
        finally:
            await peer.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
