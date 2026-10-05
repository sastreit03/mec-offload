"""One shared WebSocket endpoint, one independent sender/session per UE."""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from uuid import uuid4

from websockets.exceptions import ConnectionClosed
from .records import UE, stamp


@dataclass
class Session:
    connection_id: str
    socket: object
    outgoing: asyncio.Queue


class ControlHub:
    def __init__(self, mec):
        self.mec = mec
        mec.hub = self
        self.sessions = {}
        self.tokens = {}  # Never exported to run.json.
        self.ue_tokens = {}
        self.history = {}  # Uncapped delivery history for this run only.
        self.next_result_seq = {}

    def emit(self, ue_id, message):
        sequence = self.next_result_seq.get(ue_id, 0)
        self.next_result_seq[ue_id] = sequence + 1
        result = {**message, "result_id": str(uuid4()), "result_seq": sequence,
                  "created_unix_ns": time.time_ns(), "run_id": self.mec.log.run_id}
        self.history.setdefault(ue_id, []).append(result)
        session = self.sessions.get(ue_id)
        if session:
            session.outgoing.put_nowait(result)
        return result

    async def _sender(self, ue, session):
        try:
            while True:
                message = await session.outgoing.get()
                await session.socket.send(json.dumps(message, allow_nan=False))
                sent = stamp()
                self.mec.log.event("result_send" if "result_id" in message else "response_send",
                    ue_id=ue.ue_id, connection_id=session.connection_id,
                    task_id=message.get("task_id"), frame_id=message.get("frame_id"),
                    result_id=message.get("result_id"), result_seq=message.get("result_seq"), sent=sent)
                if message.get("type") == "offload_response":
                    self.mec.tasks[message["task_id"]].times.setdefault("response_send", sent)
        except ConnectionClosed:
            return

    async def session(self, socket):
        if socket.request.path != self.mec.cfg.network.control_path:
            await socket.close(code=1008, reason="Unknown control path")
            return
        token = socket.request.headers.get("X-MEC-Resume-Token")
        ue_id = self.tokens.get(token)
        peer = socket.remote_address[0] if socket.remote_address else "unknown"
        if ue_id is None:
            ue_id = str(uuid4())
            token = secrets.token_urlsafe(32)
            self.tokens[token], self.ue_tokens[ue_id] = ue_id, token
            self.mec.ues[ue_id] = UE(ue_id, peer)
        ue = self.mec.ues[ue_id]
        old = self.sessions.get(ue_id)
        connection_id = str(uuid4())
        session = Session(connection_id, socket, asyncio.Queue())
        self.sessions[ue_id] = session
        ue.peer_ip, ue.connection_id = peer, connection_id
        ue.connection_state, ue.disconnected_mono_ns = "connected", None
        ue.last_seen = stamp()
        # Install the new session before closing old one. Its finally block must
        # not disconnect the UE or start a timer for this newer connection.
        if old:
            await old.socket.close(code=4001, reason="Replaced by resumed connection")
        self.mec.log.event("connection_open", ue_id=ue_id, connection_id=connection_id, peer_ip=peer)
        sender = asyncio.create_task(self._sender(ue, session))
        session.outgoing.put_nowait({"type": "mec_hello", "ue_id": ue_id, "resume_token": token,
            "connection_id": connection_id, "run_id": self.mec.log.run_id,
            "protocol_version": 1, "catalog_version": self.mec.catalog.version,
            "supported_configurations": [c.model_dump() for c in self.mec.catalog.configurations],
            "message": "Media may start after acceptance while model preparation continues.",
            "queue_policy": {"max_pending_frames": self.mec.cfg.queues.max_pending_frames,
                             "overflow": "drop_oldest", "overflow_overrides_can_drop_frames": True}})
        # Conservative replay includes successfully written but unconfirmed
        # results too. Client stable-ID suppression makes retries harmless.
        for message in self.history.get(ue_id, []):
            session.outgoing.put_nowait(message)
        self.mec.background_task(self.mec.decisions.associate(ue, self.mec.cfg.timeouts.decision_s))
        try:
            async for raw in socket:
                message = None
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("Expected a JSON object")
                    ue.last_seen = stamp()
                    typ = message.get("type")
                    if typ == "offload_request":
                        response = await self.mec.offload(ue, message)
                        session.outgoing.put_nowait(response)
                    elif typ == "task_end":
                        if message.get("ue_id") != ue_id:
                            raise ValueError("Identity mismatch")
                        self.mec.task_end(ue_id, message["task_id"], message["final_frame_id"])
                    elif typ == "task_cancel":
                        # Explicit UE cancellation is not a scheduler's use of
                        # can_drop permission; a user can always stop their own task.
                        rt = self.mec.owned(ue_id, message["task_id"])
                        self.mec.terminate(rt.row.task_id, "cancelled", "ue_cancelled")
                    elif typ == "ping":
                        session.outgoing.put_nowait({"type": "pong", "sent_unix_ns": time.time_ns()})
                    else:
                        raise ValueError("Unknown message type")
                except (ValueError, KeyError, TypeError) as exc:
                    session.outgoing.put_nowait({"type": "error", "ue_id": ue_id,
                        "client_seq": message.get("client_seq") if isinstance(message, dict) else None,
                        "reason": "invalid_message", "detail": str(exc)})
        except ConnectionClosed:
            pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if self.sessions.get(ue_id) is session:
                del self.sessions[ue_id]
                ue.connection_state = "disconnected"
                ue.disconnected_mono_ns = time.monotonic_ns()
                self.mec.log.event("connection_close", ue_id=ue_id, connection_id=connection_id)

    async def close(self):
        await asyncio.gather(*(s.socket.close(code=1001, reason="MEC shutdown") for s in list(self.sessions.values())), return_exceptions=True)
