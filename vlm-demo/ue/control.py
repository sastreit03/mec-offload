from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from models import NetworkConfig, SharedState, VideoConfig


LOG = logging.getLogger("ue.control")


class ControlClient:
    """Reliable UE <-> MEC control channel.

    Video never goes through this socket. It carries only small JSON messages:
    hello, stream configuration, question updates, and VLM results.
    """

    def __init__(
        self,
        network: NetworkConfig,
        state: SharedState,
        initial_video_config: VideoConfig,
        ue_ip: str
    ) -> None:
        self._network = network
        self._state = state
        self._latest_video_config = initial_video_config.model_copy(deep=True)
        self._outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._ue_ip = ue_ip

    async def run(self) -> None:
        # Current websockets asyncio API supports automatic reconnect when connect()
        # is used as an async iterator. proxy=None is intentional: local/private 5G
        # traffic must not accidentally follow HTTP proxy environment variables.
        try:
            async for ws in connect(
                self._network.control_ws_url,
                open_timeout=5,
                ping_interval=10,
                ping_timeout=10,
                close_timeout=3,
                max_size=1_048_576,
                compression=None,
                proxy=None,
                local_addr=(self._ue_ip, 0),
            ):
                self._state.set_control_connected(True, None)
                LOG.info("Control channel connected to %s", self._network.control_ws_url)
                try:
                    self._discard_queued_messages()
                    await self._send_initial_state(ws)

                    sender = asyncio.create_task(self._sender(ws))
                    receiver = asyncio.create_task(self._receiver(ws))
                    done, pending = await asyncio.wait(
                        {sender, receiver},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        exc = task.exception()
                        if exc:
                            raise exc
                except ConnectionClosed as exc:
                    LOG.warning("Control channel closed: %s", exc)
                except Exception as exc:  # noqa: BLE001 - reconnect loop must survive
                    LOG.warning("Control connection error: %s", exc)
                finally:
                    self._state.set_control_connected(False, "disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # fatal configuration / connection setup error
            self._state.set_control_connected(False, str(exc))
            LOG.exception("Control client stopped: %s", exc)

    async def send_question(self, text: str) -> dict[str, Any]:
        message = self._state.new_question(text)
        await self._queue_latest(message)
        return message

    async def publish_stream_config(self, cfg: VideoConfig) -> None:
        self._latest_video_config = cfg.model_copy(deep=True)
        await self._queue_latest(self._stream_config_message())

    async def _sender(self, ws: Any) -> None:
        while True:
            message = await self._outgoing.get()
            await ws.send(json.dumps(message, separators=(",", ":")))

    async def _receiver(self, ws: Any) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("JSON message must be an object")
            except Exception as exc:  # malformed MEC message shouldn't kill video
                LOG.warning("Ignoring malformed MEC control message: %s", exc)
                continue
            self._state.set_control_message(message)

    async def _send_initial_state(self, ws: Any) -> None:
        hello = {
            "type": "ue_hello",
            "protocol_version": 1,
            "sent_unix_ns": time.time_ns(),
        }
        await ws.send(json.dumps(hello, separators=(",", ":")))
        await ws.send(json.dumps(self._stream_config_message(), separators=(",", ":")))

        active = self._state.snapshot()["control"]["active_question"]
        if active is not None:
            await ws.send(json.dumps(active, separators=(",", ":")))

    def _stream_config_message(self) -> dict[str, Any]:
        cfg = self._latest_video_config
        return {
            "type": "stream_config",
            "sent_unix_ns": time.time_ns(),
            "video": {
                "codec": "H264",
                "rtp_payload_type": 96,
                "fps": cfg.fps,
                "width": cfg.width,
                "height": cfg.height,
                "bitrate_kbps": cfg.bitrate_kbps,
                "gop_frames": cfg.gop_frames,
                "b_frames": 0,
                "rtp_mtu": cfg.rtp_mtu,
                "video_port": self._network.video_port,
            },
        }

    async def _queue_latest(self, message: dict[str, Any]) -> None:
        if self._outgoing.full():
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self._outgoing.put(message)

    def _discard_queued_messages(self) -> None:
        # Initial-state synchronization sends the latest config and current question,
        # so old disconnected-period messages are redundant.
        while True:
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                return
