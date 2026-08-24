from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from models import FrameBuffer, SharedState, VLMConfig


LOG = logging.getLogger("mec.control")
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class ControlHub:
    """Single-UE bidirectional WebSocket control channel.

    The UE initiates the TCP/WebSocket connection. The same connection carries
    questions/configuration toward MEC and VLM results back toward UE.
    """

    def __init__(
        self,
        state: SharedState,
        frame_buffer: FrameBuffer,
        vlm_config: VLMConfig,
        on_question_changed: Callable[[], None] | None = None,
    ) -> None:
        self._state = state
        self._frames = frame_buffer
        self._vlm_cfg = vlm_config
        self._on_question_changed = on_question_changed
        self._outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._session_lock = asyncio.Lock()

    async def websocket_session(self, ws: WebSocket) -> None:
        # One UE is expected. Serialize sessions so an accidental second UE does
        # not race the first one for result delivery.
        async with self._session_lock:
            await ws.accept()
            peer = None
            if ws.client is not None:
                peer = f"{ws.client.host}:{ws.client.port}"
            self._discard_outgoing()
            self._state.set_control_connected(True, peer=peer, error=None)
            LOG.info("UE control channel connected: %s", peer or "unknown")

            sender = asyncio.create_task(self._sender(ws))
            try:
                await self.send(
                    {
                        "type": "mec_hello",
                        "protocol_version": 1,
                        "sent_unix_ns": time.time_ns(),
                    }
                )
                while True:
                    raw = await ws.receive_text()
                    try:
                        message = json.loads(raw)
                        if not isinstance(message, dict):
                            raise ValueError("JSON message must be an object")
                    except Exception as exc:
                        LOG.warning("Ignoring malformed UE JSON: %s", exc)
                        await self.send(
                            {
                                "type": "error",
                                "error": "malformed_json",
                                "detail": str(exc),
                            }
                        )
                        continue
                    await self._handle_message(message)
            except WebSocketDisconnect:
                LOG.info("UE control channel disconnected")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("UE control channel error: %s", exc)
                self._state.set_control_connected(False, error=str(exc))
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                self._state.set_control_connected(False, error="disconnected")

    async def _sender(self, ws: WebSocket) -> None:
        while True:
            message = await self._outgoing.get()
            await ws.send_text(json.dumps(message, separators=(",", ":")))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type in {"ue_hello", "stream_config"}:
            self._state.set_control_message(message)
            if msg_type == "stream_config":
                await self.send(
                    {
                        "type": "stream_config_ack",
                        "received_unix_ns": time.time_ns(),
                        "video": message.get("video", {}),
                    }
                )
            return

        if msg_type == "question":
            question_id = message.get("question_id")
            text = message.get("text")
            if not isinstance(question_id, int) or not isinstance(text, str) or not text.strip():
                await self.send(
                    {
                        "type": "error",
                        "error": "invalid_question",
                        "detail": "question_id must be int and text must be non-empty",
                    }
                )
                return

            changed = self._state.set_question(message)
            if changed:
                LOG.info("Active question #%d: %s", question_id, text.strip())
                if self._vlm_cfg.clear_buffer_on_new_question:
                    self._frames.clear()
                if self._on_question_changed is not None:
                    self._on_question_changed()
            await self.send(
                {
                    "type": "question_ack",
                    "question_id": question_id,
                    "received_unix_ns": time.time_ns(),
                }
            )
            return

        self._state.set_control_message(message)
        LOG.debug("Ignoring unsupported UE message type: %s", msg_type)

    async def send(self, message: dict[str, Any]) -> None:
        # Results are small and infrequent. If the queue is somehow saturated,
        # discard the oldest stale message rather than blocking the inference loop.
        if self._outgoing.full():
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self._outgoing.put(message)

    def _discard_outgoing(self) -> None:
        while True:
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                return
