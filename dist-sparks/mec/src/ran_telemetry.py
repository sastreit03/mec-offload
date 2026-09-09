from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import grpc

from models import RANTelemetryConfig


LOG = logging.getLogger("mec.ran_telemetry")


# The gRPC service and method names must match the generic service registered
# by ran_telemetry_server.py in the ran_telemetry_xapp container.
_GRPC_SERVICE = "ran.telemetry.RANTelemetry"
_STREAM_METHOD = f"/{_GRPC_SERVICE}/StreamTelemetry"


def _json_serialize(value: dict[str, Any]) -> bytes:
    """Encode a Python dictionary as the JSON request expected by the xApp."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _json_deserialize(payload: bytes) -> dict[str, Any]:
    """Decode one JSON telemetry response received from the xApp."""
    value = json.loads(payload.decode("utf-8"))

    if not isinstance(value, dict):
        raise ValueError("gRPC telemetry response must be a JSON object")

    return value


class RANTelemetryState:
    """Thread-safe storage for the most recent RAN telemetry snapshot.

    The gRPC client writes this state whenever a new telemetry message arrives.
    Other MEC components, such as the dashboard and future offloading
    controller, read snapshots from it without communicating with the xApp
    directly.
    """

    def __init__(self, stale_after_s: float) -> None:
        self._lock = threading.RLock()
        self._stale_after_s = stale_after_s

        # gRPC connection state.
        self._connected = False
        self._peer: str | None = None
        self._last_error: str | None = None

        # Number of complete gRPC telemetry snapshots received by the MEC.
        self._messages_received = 0

        # Local timestamps for the most recent gRPC message.
        #
        # Unix time is useful for logging/experiment records.
        # Monotonic time is used when deciding how old a snapshot is.
        self._last_update_unix_ns: int | None = None
        self._last_update_monotonic_ns: int | None = None

        # Latest complete JSON telemetry object received from the xApp.
        self._latest: dict[str, Any] | None = None

    def set_connected(self, connected: bool, *,
        peer: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update the gRPC connection status."""

        with self._lock:
            self._connected = connected

            if connected:
                self._peer = peer
                self._last_error = None
            else:
                self._peer = None
                self._last_error = error

    def update(self, telemetry: dict[str, Any]) -> None:
        """Store a newly received complete telemetry snapshot."""

        now_unix_ns = time.time_ns()
        now_monotonic_ns = time.monotonic_ns()

        with self._lock:
            # Make a top-level copy so the state does not retain the exact
            # dictionary object owned by the gRPC receive loop.
            self._latest = dict(telemetry)

            self._messages_received += 1
            self._last_update_unix_ns = now_unix_ns
            self._last_update_monotonic_ns = now_monotonic_ns

    def snapshot(self) -> dict[str, Any]:
        """Return the current RAN telemetry state.

        No throughput, rate, or counter deltas are calculated here yet.
        This returns the latest raw MAC/RLC/PDCP/GTP snapshot plus metadata
        describing the MEC-side gRPC connection.
        """

        with self._lock:
            latest = (dict(self._latest) if self._latest is not None else None)
            connected = self._connected
            peer = self._peer
            last_error = self._last_error
            messages_received = self._messages_received
            last_update_unix_ns = self._last_update_unix_ns
            last_update_monotonic_ns = self._last_update_monotonic_ns

        # Calculate only the age of the locally cached snapshot.
        #
        # This is not a telemetry-rate or throughput delta. It simply allows
        # the rest of the MEC application to know whether its cached RAN state
        # is current or stale.
        age_ms: float | None = None

        if last_update_monotonic_ns is not None:
            age_ms = (time.monotonic_ns() - last_update_monotonic_ns) / 1e6

        stale = (age_ms is None or age_ms > self._stale_after_s * 1000.0)

        return {
            "connected": connected,
            "peer": peer,
            "last_error": last_error,
            "messages_received": messages_received,
            "last_update_unix_ns": last_update_unix_ns,
            "age_ms": (round(age_ms, 1) if age_ms is not None else None),
            "stale": stale,
            "telemetry": latest,
        }

    def get_latest(self) -> dict[str, Any] | None:
        """Return only the latest raw telemetry payload.

        This is the method the future offloading controller can use when it
        wants the RAN state without the connection/status metadata.
        """

        with self._lock:
            if self._latest is None:
                return None

            return dict(self._latest)


class RANTelemetryClient:
    """Persistent asynchronous gRPC client for the RAN telemetry xApp.

    The client connects to ran_telemetry_xapp, starts its StreamTelemetry RPC,
    and continuously stores the most recent message in RANTelemetryState.

    If the xApp or network becomes unavailable, the client marks the telemetry
    connection disconnected, waits for reconnect_delay_s, and tries again.

    Failure of this client does not terminate the MEC video/VLM application.
    """

    def __init__(self, config: RANTelemetryConfig, state: RANTelemetryState) -> None:
        self._cfg = config.model_copy(deep=True)
        self._state = state

        # Example:
        #   192.168.1.100:50051
        #
        # When the xApp is on another physical PC, host should be the reachable
        # IP address of that PC, whose Docker host publishes TCP port 50051.
        self._target = (f"{self._cfg.host}:{self._cfg.port}")

    async def run(self) -> None:
        """Run the telemetry connection until this asyncio task is cancelled."""

        if not self._cfg.enabled:
            LOG.info("RAN telemetry client is disabled")
            return

        LOG.info("RAN telemetry client configured for %s", self._target)

        while True:
            try:
                await self._receive_stream()

            except asyncio.CancelledError:
                # Application shutdown should stop immediately rather than
                # entering the reconnect path.
                raise

            except grpc.aio.AioRpcError as exc:
                error_text = (
                    f"{exc.code().name}: "
                    f"{exc.details() or ''}"
                ).strip()

                self._state.set_connected(False, error=error_text)

                LOG.warning(
                    "RAN telemetry gRPC connection failed: %s",
                    error_text,
                )

            except Exception as exc:  # noqa: BLE001
                self._state.set_connected(False, error=str(exc))

                LOG.exception("Unexpected RAN telemetry client error: %s", exc)

            # Do not spin aggressively when the xApp, its host, or the
            # inter-PC network is unavailable.
            LOG.info(
                "Retrying RAN telemetry connection in %.1f s",
                self._cfg.reconnect_delay_s,
            )

            await asyncio.sleep(self._cfg.reconnect_delay_s)

    async def _receive_stream(self) -> None:
        """Open one gRPC channel and consume StreamTelemetry until it ends."""

        LOG.info("Connecting to RAN telemetry gRPC server at %s", self._target)

        # grpc.aio keeps all gRPC network work asynchronous so it does not block
        # FastAPI, the WebSocket control path, or the inference scheduler.
        async with grpc.aio.insecure_channel(self._target) as channel:

            # Wait until the underlying gRPC channel has established a usable
            # connection. A timeout ensures we return to our retry loop if the
            # remote xApp cannot be reached.
            await asyncio.wait_for(
                channel.channel_ready(),
                timeout=self._cfg.connect_timeout_s,
            )

            LOG.info("Connected to RAN telemetry gRPC server at %s", self._target)

            self._state.set_connected(True, peer=self._target)

            # Because this first API uses JSON over generic gRPC handlers rather
            # than generated protobuf classes, construct the streaming RPC
            # directly from its fully qualified method path.
            stream_rpc = channel.unary_stream(
                _STREAM_METHOD,
                request_serializer=_json_serialize,
                response_deserializer=_json_deserialize,
            )

            # Tell the xApp how often it should send a snapshot to the MEC.
            #
            # The FlexRIC xApp may still receive indications every 10 ms;
            # this independently controls the xApp -> MEC gRPC stream rate.
            request = {"interval_s": self._cfg.stream_interval_s}

            call = stream_rpc(request)

            try:
                async for telemetry in call:

                    # Receipt of a valid message confirms that the stream is
                    # actively delivering data.
                    self._state.set_connected(True, peer=self._target)

                    self._state.update(telemetry)

            finally:
                # If the server ends the stream normally, mark the connection
                # unavailable before the outer run() loop attempts a reconnect.
                self._state.set_connected(False, error="telemetry stream ended")
