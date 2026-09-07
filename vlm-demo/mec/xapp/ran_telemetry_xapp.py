#!/usr/bin/env python3
"""
RAN telemetry xApp with gRPC server.

Purpose
-------
This xApp:

1. Connects to the FlexRIC nearRT-RIC.
2. Subscribes to the MAC, RLC, PDCP, and GTP service models.
3. Extracts selected scalar telemetry from each indication.
4. Stores the latest telemetry in shared process memory.
5. Exposes that telemetry to external applications through a gRPC server.

The MEC server will later act as the gRPC client.

Notes
-----
The gRPC API in this first version intentionally uses JSON-encoded byte
messages instead of generated Protocol Buffer classes. This keeps the
telemetry schema easy to modify while the useful RAN fields are still being
identified.

A later version can replace the JSON payloads with a typed .proto schema
without changing the FlexRIC subscription portion of this file.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from concurrent import futures
from typing import Any, Iterator

import grpc
import xapp_sdk as ric


# =============================================================================
# Configuration
# =============================================================================

# FlexRIC reporting interval.
XAPP_INTERVAL_NAME = os.getenv("XAPP_INTERVAL", "Interval_ms_10")

# IP address on which the gRPC server listens.
GRPC_BIND_ADDRESS = os.getenv("GRPC_BIND_ADDRESS", "0.0.0.0")

# Default gRPC port.
GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))

# Number of worker threads available to gRPC requests.
GRPC_MAX_WORKERS = int(os.getenv("GRPC_MAX_WORKERS", "4"))

# Default interval for the server-streaming telemetry RPC.
GRPC_STREAM_INTERVAL_S = float( os.getenv("GRPC_STREAM_INTERVAL_S", "1.0"))


# =============================================================================
# Global FlexRIC state
# =============================================================================

# Keep handler references alive for the lifetime of the subscriptions.
handlers: dict[str, list[Any]] = {
    "mac": [],
    "rlc": [],
    "pdcp": [],
    "gtp": [],
}

# SWIG callback objects must also remain alive while their subscriptions exist.
callbacks: dict[str, list[Any]] = {
    "mac": [],
    "rlc": [],
    "pdcp": [],
    "gtp": [],
}

# Controls application shutdown.
running = True


# =============================================================================
# Telemetry state
# =============================================================================

class TelemetryState:
    """
    Thread-safe storage for the most recent indication from each service model.

    FlexRIC invokes callbacks asynchronously, while gRPC worker threads may
    simultaneously read the telemetry. A lock prevents a gRPC reader from
    seeing a partially updated Python object.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Latest decoded values from each service model.
        self._latest: dict[str, list[dict[str, Any]]] = {
            "mac": [],
            "rlc": [],
            "pdcp": [],
            "gtp": [],
        }

        # Timestamp supplied by the FlexRIC indication.
        self._ric_timestamps: dict[str, int | None] = {
            "mac": None,
            "rlc": None,
            "pdcp": None,
            "gtp": None,
        }

        # Local wall-clock time at which each callback was processed.
        self._received_unix_ns: dict[str, int | None] = {
            "mac": None,
            "rlc": None,
            "pdcp": None,
            "gtp": None,
        }

        # Number of indications received from each service model.
        self._counts: dict[str, int] = {
            "mac": 0,
            "rlc": 0,
            "pdcp": 0,
            "gtp": 0,
        }

    def update(
        self,
        sm_name: str,
        *,
        ric_timestamp: int | None,
        records: list[dict[str, Any]],
    ) -> None:
        """Replace the latest telemetry for one service model."""

        with self._lock:
            self._latest[sm_name] = records
            self._ric_timestamps[sm_name] = ric_timestamp
            self._received_unix_ns[sm_name] = time.time_ns()
            self._counts[sm_name] += 1

    def snapshot(self) -> dict[str, Any]:
        """
        Return a JSON-safe copy of the current telemetry state.

        A server timestamp is added here so the future MEC client knows when
        the snapshot was assembled.
        """

        with self._lock:
            return {
                "server_unix_ns": time.time_ns(),
                "indication_counts": dict(self._counts),
                "ric_timestamps": dict(self._ric_timestamps),
                "received_unix_ns": dict(self._received_unix_ns),
                "mac":  [dict(record) for record in self._latest["mac"]],
                "rlc":  [dict(record) for record in self._latest["rlc"]],
                "pdcp": [dict(record) for record in self._latest["pdcp"]],
                "gtp":  [dict(record) for record in self._latest["gtp"]],
            }

telemetry_state = TelemetryState()


# =============================================================================
# General SWIG helpers
# =============================================================================

def get_attr(obj: Any, name: str, default: Any = None) -> Any:
    """
    Safely read one attribute from a SWIG object.

    Some SWIG-generated objects may not expose every field on every build.
    Returning a default keeps one unavailable field from killing an entire
    indication callback.
    """

    try:
        return getattr(obj, name)
    except Exception:
        return default


def vector_items(vector: Any) -> list[Any]:
    """
    Convert a SWIG vector into a normal Python list.

    FlexRIC indication arrays act like Python sequences but are generally
    SWIG vector proxy objects rather than normal lists.
    """

    try:
        return [vector[index] for index in range(len(vector))]
    except Exception:
        return []


def uint32_to_int32(value: Any) -> int | None:
    """
    Also interpret a possible unsigned 32-bit value as signed.

    This is retained for BSR diagnostics because the testbed occasionally
    returned values close to 2^32, such as 4294966871. We are not yet assuming
    those wrapped values have valid BSR semantics.

    Both raw and signed representations are exported to the MEC for now.
    """

    if not isinstance(value, int):
        return None

    value &= 0xFFFFFFFF

    if value >= 0x80000000:
        return value - 0x100000000

    return value


# =============================================================================
# MAC extraction
# =============================================================================

def extract_mac(ind: Any) -> list[dict[str, Any]]:
    """
    Extract per-UE MAC telemetry.

    These fields have all been observed in the current Sionna-RK FlexRIC
    Python indication structure.

    Several fields such as CQI and BSR remain exported even though their
    semantics/population require further validation.
    """

    records: list[dict[str, Any]] = []

    ue_stats = get_attr(ind, "ue_stats", [])

    for ue in vector_items(ue_stats):
        bsr_raw = get_attr(ue, "bsr")

        records.append(
            {
                # UE/radio identification.
                "rnti": get_attr(ue, "rnti"),
                "frame": get_attr(ue, "frame"),
                "slot": get_attr(ue, "slot"),

                # Modulation and coding.
                "ul_mcs1": get_attr(ue, "ul_mcs1"),
                "ul_mcs2": get_attr(ue, "ul_mcs2"),
                "dl_mcs1": get_attr(ue, "dl_mcs1"),
                "dl_mcs2": get_attr(ue, "dl_mcs2"),

                # Link reliability.
                "ul_bler": get_attr(ue, "ul_bler"),
                "dl_bler": get_attr(ue, "dl_bler"),

                # Current scheduled resource blocks.
                "ul_sched_rb": get_attr(ue, "ul_sched_rb"),
                "dl_sched_rb": get_attr(ue, "dl_sched_rb"),

                # Cumulative PRB usage.
                "ul_aggr_prb": get_attr(ue, "ul_aggr_prb"),
                "dl_aggr_prb": get_attr(ue, "dl_aggr_prb"),

                # Cumulative retransmission PRBs.
                "ul_aggr_retx_prb": get_attr(ue, "ul_aggr_retx_prb"),
                "dl_aggr_retx_prb": get_attr(ue, "dl_aggr_retx_prb"),

                # Current and cumulative transport-block sizes.
                "ul_curr_tbs": get_attr(ue, "ul_curr_tbs"),
                "dl_curr_tbs": get_attr(ue, "dl_curr_tbs"),
                "ul_aggr_tbs": get_attr(ue, "ul_aggr_tbs"),
                "dl_aggr_tbs": get_attr(ue, "dl_aggr_tbs"),

                # MAC SDU counters.
                "ul_aggr_bytes_sdus": get_attr(ue, "ul_aggr_bytes_sdus"),
                "dl_aggr_bytes_sdus": get_attr(ue, "dl_aggr_bytes_sdus"),
                "ul_aggr_sdus": get_attr(ue, "ul_aggr_sdus"),
                "dl_aggr_sdus": get_attr(ue, "dl_aggr_sdus"),

                # Radio-quality information.
                "pusch_snr": get_attr(ue, "pusch_snr"),
                "pucch_snr": get_attr(ue, "pucch_snr"),
                "wb_cqi": get_attr(ue,"wb_cqi"),

                # Buffer/power reporting.
                #
                # BSR is still being validated, so preserve both
                # representations.
                "bsr_raw": bsr_raw,
                "bsr_signed_i32": uint32_to_int32(bsr_raw),
                "phr": get_attr(ue, "phr"),

                # Number of HARQ processes exposed by this SM.
                #
                # We deliberately do not serialize the raw dl_harq/ul_harq
                # SWIG objects.
                "ul_num_harq": get_attr(ue, "ul_num_harq"),
                "dl_num_harq": get_attr(ue, "dl_num_harq"),
            }
        )

    return records


# =============================================================================
# RLC extraction
# =============================================================================

def extract_rlc(
    ind: Any,
) -> list[dict[str, Any]]:
    """
    Extract per-radio-bearer RLC telemetry.

    The TX-buffer occupancy and TX SDU head-of-line wait time were verified to
    become non-zero under downlink iperf3 load in the current testbed.
    """

    records: list[dict[str, Any]] = []

    rb_stats = get_attr(ind, "rb_stats", [])

    for rb in vector_items(rb_stats):
        records.append(
            {
                # Bearer identification.
                "rnti": get_attr(rb, "rnti"),
                "rbid": get_attr(rb, "rbid"),
                "mode": get_attr(rb, "mode"),

                # RLC buffers.
                "txbuf_occ_bytes": get_attr(rb, "txbuf_occ_bytes"),
                "txbuf_occ_pkts": get_attr(rb, "txbuf_occ_pkts"),
                "rxbuf_occ_bytes": get_attr(rb, "rxbuf_occ_bytes"),
                "rxbuf_occ_pkts": get_attr(rb, "rxbuf_occ_pkts"),

                # TX PDU counters.
                "txpdu_pkts": get_attr(rb, "txpdu_pkts"),
                "txpdu_bytes": get_attr(rb, "txpdu_bytes"),
                "txpdu_dd_pkts": get_attr(rb, "txpdu_dd_pkts"),
                "txpdu_dd_bytes": get_attr(rb, "txpdu_dd_bytes"),
                "txpdu_retx_pkts": get_attr(rb, "txpdu_retx_pkts"),
                "txpdu_retx_bytes": get_attr(rb, "txpdu_retx_bytes"),
                "txpdu_segmented": get_attr(rb, "txpdu_segmented"),
                "txpdu_status_pkts": get_attr(rb, "txpdu_status_pkts"),
                "txpdu_status_bytes": get_attr(rb, "txpdu_status_bytes"),

                # RX PDU counters.
                "rxpdu_pkts": get_attr(rb, "rxpdu_pkts"),
                "rxpdu_bytes": get_attr(rb, "rxpdu_bytes"),
                "rxpdu_dup_pkts": get_attr(rb, "rxpdu_dup_pkts"),
                "rxpdu_dup_bytes": get_attr(rb, "rxpdu_dup_bytes"),
                "rxpdu_dd_pkts": get_attr(rb, "rxpdu_dd_pkts"),
                "rxpdu_dd_bytes": get_attr(rb, "rxpdu_dd_bytes"),
                "rxpdu_ow_pkts": get_attr(rb, "rxpdu_ow_pkts"),
                "rxpdu_ow_bytes": get_attr(rb, "rxpdu_ow_bytes"),
                "rxpdu_status_pkts": get_attr(rb, "rxpdu_status_pkts"),
                "rxpdu_status_bytes": get_attr(rb, "rxpdu_status_bytes"),

                # SDU counters.
                "txsdu_pkts": get_attr(rb, "txsdu_pkts"),
                "txsdu_bytes": get_attr(rb, "txsdu_bytes"),
                "rxsdu_pkts": get_attr(rb, "rxsdu_pkts"),
                "rxsdu_bytes": get_attr(rb, "rxsdu_bytes"),
                "rxsdu_dd_pkts": get_attr(rb, "rxsdu_dd_pkts"),
                "rxsdu_dd_bytes": get_attr(rb, "rxsdu_dd_bytes"),

                # Delay / queueing telemetry.
                "txsdu_avg_time_to_tx": get_attr(rb, "txsdu_avg_time_to_tx"),
                "txsdu_wt_us": get_attr(rb, "txsdu_wt_us"),
                "txpdu_wt_ms": get_attr(rb, "txpdu_wt_ms"),
            }
        )

    return records


# =============================================================================
# PDCP extraction
# =============================================================================

def extract_pdcp(
    ind: Any,
) -> list[dict[str, Any]]:
    """Extract per-radio-bearer PDCP counters."""

    records: list[dict[str, Any]] = []

    rb_stats = get_attr(ind, "rb_stats", [])

    for rb in vector_items(rb_stats):
        records.append(
            {
                # Bearer identification.
                "rnti": get_attr(rb, "rnti"),
                "rbid": get_attr(rb, "rbid"),
                "mode": get_attr(rb, "mode"),

                # TX PDCP counters.
                "txpdu_pkts": get_attr(rb, "txpdu_pkts"),
                "txpdu_bytes": get_attr(rb, "txpdu_bytes"),
                "txpdu_sn": get_attr(rb, "txpdu_sn"),

                # RX PDCP counters.
                "rxpdu_pkts": get_attr(rb, "rxpdu_pkts"),
                "rxpdu_bytes": get_attr(rb, "rxpdu_bytes"),
                "rxpdu_sn": get_attr(rb, "rxpdu_sn"),

                # Out-of-order and discarded/duplicate reception.
                "rxpdu_oo_pkts": get_attr(rb, "rxpdu_oo_pkts"),
                "rxpdu_oo_bytes": get_attr(rb, "rxpdu_oo_bytes"),
                "rxpdu_dd_pkts": get_attr(rb, "rxpdu_dd_pkts"),
                "rxpdu_dd_bytes": get_attr(rb, "rxpdu_dd_bytes"),
                "rxpdu_ro_count": get_attr(rb, "rxpdu_ro_count"),

                # SDU counters.
                "txsdu_pkts": get_attr(rb, "txsdu_pkts"),
                "txsdu_bytes": get_attr(rb, "txsdu_bytes"),
                "rxsdu_pkts": get_attr(rb, "rxsdu_pkts"),
                "rxsdu_bytes": get_attr(rb, "rxsdu_bytes"),
            }
        )

    return records


# =============================================================================
# GTP extraction
# =============================================================================

def extract_gtp(ind: Any) -> list[dict[str, Any]]:
    """
    Extract GTP-NGU context.

    Unlike MAC/RLC/PDCP counters, these values are primarily session/context
    information and normally remain stable while the PDU session exists.
    """

    records: list[dict[str, Any]] = []

    gtp_stats = get_attr(ind, "gtp_stats", [])

    for tunnel in vector_items(gtp_stats):
        records.append(
            {
                "rnti": get_attr(tunnel, "rnti"),
                "qfi": get_attr(tunnel, "qfi"),
                "teid_gnb": get_attr(tunnel, "teidgnb"),
                "teid_upf": get_attr(tunnel, "teidupf"),
            }
        )

    return records


# =============================================================================
# FlexRIC callbacks
# =============================================================================

class MACCallback(ric.mac_cb):
    """Receive MAC indications from FlexRIC."""

    def __init__(self) -> None:
        ric.mac_cb.__init__(self)

    def handle(self, ind: Any) -> None:
        telemetry_state.update(
            "mac",
            ric_timestamp=get_attr(ind, "tstamp"),
            records=extract_mac(ind),
        )


class RLCCallback(ric.rlc_cb):
    """Receive RLC indications from FlexRIC."""

    def __init__(self) -> None:
        ric.rlc_cb.__init__(self)

    def handle(self, ind: Any) -> None:
        telemetry_state.update(
            "rlc",
            ric_timestamp=get_attr(ind, "tstamp"),
            records=extract_rlc(ind),
        )


class PDCPCallback(ric.pdcp_cb):
    """Receive PDCP indications from FlexRIC."""

    def __init__(self) -> None:
        ric.pdcp_cb.__init__(self)

    def handle(self, ind: Any) -> None:
        telemetry_state.update(
            "pdcp",
            ric_timestamp=get_attr(ind, "tstamp"),
            records=extract_pdcp(ind),
        )


class GTPCallback(ric.gtp_cb):
    """Receive GTP indications from FlexRIC."""

    def __init__(self) -> None:
        ric.gtp_cb.__init__(self)

    def handle(self, ind: Any) -> None:
        telemetry_state.update(
            "gtp",
            ric_timestamp=get_attr(ind, "tstamp"),
            records=extract_gtp(ind),
        )


# =============================================================================
# JSON-over-gRPC serialization
# =============================================================================

def json_serialize(value: dict[str, Any]) -> bytes:
    """Serialize one gRPC response as compact UTF-8 JSON."""
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def json_deserialize(payload: bytes) -> dict[str, Any]:
    """
    Deserialize an incoming JSON gRPC request.

    Empty requests are accepted and converted to {} so GetLatestTelemetry
    does not require the client to send any parameters.
    """

    if not payload:
        return {}

    value = json.loads(payload.decode("utf-8"))

    if not isinstance(value, dict):
        raise ValueError("gRPC request JSON must be an object")

    return value


# =============================================================================
# gRPC service
# =============================================================================

class RANTelemetryGrpcService:
    """
    Implementation of the northbound RAN telemetry gRPC API.

    Current RPCs
    ------------

    GetLatestTelemetry:
        Unary request -> unary response.
        Returns one complete snapshot immediately.

    StreamTelemetry:
        Unary request -> server stream.
        Repeatedly returns the latest snapshot at the requested interval.

    Health:
        Unary request -> unary response.
        Lightweight readiness/status check.
    """

    def get_latest_telemetry(
        self,
        request: dict[str, Any],
        context: grpc.ServicerContext,
    ) -> dict[str, Any]:
        """Return the most recent telemetry snapshot."""

        return telemetry_state.snapshot()

    def stream_telemetry(
        self,
        request: dict[str, Any],
        context: grpc.ServicerContext,
    ) -> Iterator[dict[str, Any]]:
        """
        Stream telemetry snapshots until the gRPC client disconnects.

        A client may later request a custom interval using:

            {"interval_s": 0.5}

        The server enforces a minimum of 10 ms to avoid accidental busy loops.
        """

        requested_interval = request.get("interval_s", GRPC_STREAM_INTERVAL_S)

        try:
            interval_s = float(requested_interval)
        except (TypeError, ValueError):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "interval_s must be numeric",
            )
            return

        if interval_s < 0.01:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "interval_s must be >= 0.01",
            )
            return

        while context.is_active():
            yield telemetry_state.snapshot()
            time.sleep(interval_s)

    def health(
        self,
        request: dict[str, Any],
        context: grpc.ServicerContext,
    ) -> dict[str, Any]:
        """Return basic gRPC/xApp status."""

        snapshot = telemetry_state.snapshot()

        return {
            "status": "ready",
            "server_unix_ns": time.time_ns(),
            "indication_counts":
                snapshot["indication_counts"],
        }


# =============================================================================
# gRPC server construction
# =============================================================================

def create_grpc_server() -> grpc.Server:
    """
    Create the gRPC server and register the service methods.

    We use generic handlers for now because the first version transports JSON
    rather than generated protobuf messages.
    """

    service = RANTelemetryGrpcService()

    server = grpc.server(
        futures.ThreadPoolExecutor(
            max_workers=GRPC_MAX_WORKERS
        )
    )

    method_handlers = {
        "GetLatestTelemetry":
            grpc.unary_unary_rpc_method_handler(
                service.get_latest_telemetry,
                request_deserializer=json_deserialize,
                response_serializer=json_serialize,
            ),

        "StreamTelemetry":
            grpc.unary_stream_rpc_method_handler(
                service.stream_telemetry,
                request_deserializer=json_deserialize,
                response_serializer=json_serialize,
            ),

        "Health":
            grpc.unary_unary_rpc_method_handler(
                service.health,
                request_deserializer=json_deserialize,
                response_serializer=json_serialize,
            ),
    }

    generic_handler = (
        grpc.method_handlers_generic_handler(
            "ran.telemetry.RANTelemetry",
            method_handlers,
        )
    )

    server.add_generic_rpc_handlers((generic_handler,))

    listen_address = (f"{GRPC_BIND_ADDRESS}:{GRPC_PORT}")

    bound_port = server.add_insecure_port(listen_address)

    if bound_port == 0:
        raise RuntimeError(
            f"Unable to bind gRPC server to "
            f"{listen_address}"
        )

    return server


# =============================================================================
# FlexRIC subscription management
# =============================================================================

def get_reporting_interval() -> Any:
    """Resolve the configured FlexRIC reporting interval."""

    interval = getattr(ric, XAPP_INTERVAL_NAME, None)

    if interval is None:
        available = sorted(
            name
            for name in dir(ric)
            if name.startswith("Interval_")
        )

        raise RuntimeError(
            f"xapp_sdk has no interval "
            f"{XAPP_INTERVAL_NAME!r}. "
            f"Available intervals: {available}"
        )

    return interval


def subscribe_node(node: Any, interval: Any,) -> None:
    """
    Subscribe one E2 node to all four desired service models.
    """

    subscriptions = (
        ("mac", MACCallback, ric.report_mac_sm),
        ("rlc", RLCCallback, ric.report_rlc_sm),
        ("pdcp", PDCPCallback, ric.report_pdcp_sm),
        ("gtp",  GTPCallback, ric.report_gtp_sm),
    )

    print(f"Subscribing to E2 node: {node.id}")

    for (sm_name, callback_class, report_function,) in subscriptions:

        try:
            callback = callback_class()

            # Keep the SWIG callback alive.
            callbacks[sm_name].append(callback)

            handler = report_function(node.id, interval, callback)

            handlers[sm_name].append(handler)

            print(
                f"  subscribed: "
                f"{sm_name.upper()} "
                f"(handler={handler})"
            )

        except Exception as exc:
            print(
                f"  unable to subscribe to "
                f"{sm_name.upper()}: {exc}",
                file=sys.stderr,
            )


def remove_subscriptions() -> None:
    """Remove all active FlexRIC subscriptions."""

    removal_functions = {
        "mac": ric.rm_report_mac_sm,
        "rlc": ric.rm_report_rlc_sm,
        "pdcp": ric.rm_report_pdcp_sm,
        "gtp": ric.rm_report_gtp_sm,
    }

    print("Removing FlexRIC subscriptions...")

    for sm_name, sm_handlers in handlers.items():

        remove_function = (removal_functions[sm_name])

        for handler in sm_handlers:
            try:
                remove_function(handler)

                print(
                    f"  removed "
                    f"{sm_name.upper()} "
                    f"subscription "
                    f"{handler}"
                )

            except Exception as exc:
                print(
                    f"  error removing "
                    f"{sm_name.upper()} "
                    f"subscription: {exc}",
                    file=sys.stderr,
                )


# =============================================================================
# Shutdown handling
# =============================================================================

def signal_handler(signum: int, frame: Any) -> None:
    """
    Tell the main thread to begin graceful shutdown.

    Actual gRPC and FlexRIC cleanup occurs in main().
    """

    global running

    print(
        f"\nReceived signal {signum}; "
        "shutting down..."
    )

    running = False


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    global running

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ---------------------------------------------------------------------
    # Initialize FlexRIC.
    # ---------------------------------------------------------------------

    print("Initializing FlexRIC xApp...")

    ric.init()

    interval = get_reporting_interval()

    print(
        f"Using reporting interval: "
        f"{XAPP_INTERVAL_NAME}"
    )

    nodes = ric.conn_e2_nodes()

    print(
        f"Connected E2 nodes: "
        f"{len(nodes)}"
    )

    if len(nodes) == 0:
        raise RuntimeError(
            "No E2 nodes are currently "
            "connected to the nearRT-RIC"
        )

    # Create subscriptions before advertising the gRPC service.
    for node in nodes:
        subscribe_node(node, interval)

    # ---------------------------------------------------------------------
    # Start northbound gRPC service.
    # ---------------------------------------------------------------------

    grpc_server = create_grpc_server()

    grpc_server.start()

    print()
    print("RAN telemetry gRPC server started.")
    print(
        f"Listening on "
        f"{GRPC_BIND_ADDRESS}:"
        f"{GRPC_PORT}"
    )
    print()
    print("Available RPCs:")
    print(
        "  /ran.telemetry.RANTelemetry/"
        "GetLatestTelemetry"
    )
    print(
        "  /ran.telemetry.RANTelemetry/"
        "StreamTelemetry"
    )
    print(
        "  /ran.telemetry.RANTelemetry/"
        "Health"
    )
    print()
    print("Press Ctrl+C to stop.")

    # ---------------------------------------------------------------------
    # Main process lifetime.
    # ---------------------------------------------------------------------

    try:
        while running:
            time.sleep(1)

    finally:
        print("Stopping gRPC server...")

        # Allow active RPCs a short grace period.
        grpc_server.stop(grace=2)

        remove_subscriptions()

        print("ran_telemetry_xapp stopped.")


if __name__ == "__main__":
    main()