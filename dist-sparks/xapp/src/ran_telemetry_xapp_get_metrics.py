#!/usr/bin/env python3

import os
import signal
import sys
import threading
import time

import xapp_sdk as ric


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INTERVAL_NAME = os.getenv("XAPP_INTERVAL", "Interval_ms_10")
PRINT_INTERVAL_S = float(os.getenv("PRINT_INTERVAL_S", "1.0"))


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

handlers = {
    "mac": [],
    "rlc": [],
    "pdcp": [],
    "gtp": [],
}

callbacks = {
    "mac": [],
    "rlc": [],
    "pdcp": [],
    "gtp": [],
}

latest = {
    "mac": [],
    "rlc": [],
    "pdcp": [],
    "gtp": [],
}

indication_counts = {
    "mac": 0,
    "rlc": 0,
    "pdcp": 0,
    "gtp": 0,
}

state_lock = threading.Lock()
running = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def attr(obj, name, default=None):
    """Safely read a SWIG object attribute."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def vector_items(vector):
    """Convert a SWIG vector to a normal Python list."""
    try:
        return [vector[i] for i in range(len(vector))]
    except Exception:
        return []


def uint32_to_int32(value):
    """
    Also show BSR as a signed 32-bit value.

    This is diagnostic only. We observed a BSR value close to 2^32,
    so this helps determine whether OAI/SWIG is exposing a signed value
    through an unsigned Python representation.
    """
    if not isinstance(value, int):
        return None

    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


# ---------------------------------------------------------------------------
# Extract service-model values
# ---------------------------------------------------------------------------

def extract_mac(ind):
    result = []

    for ue in vector_items(attr(ind, "ue_stats", [])):
        bsr_raw = attr(ue, "bsr")

        result.append({
            "rnti": attr(ue, "rnti"),
            "frame": attr(ue, "frame"),
            "slot": attr(ue, "slot"),

            "ul_mcs": attr(ue, "ul_mcs1"),
            "dl_mcs": attr(ue, "dl_mcs1"),

            "ul_bler": attr(ue, "ul_bler"),
            "dl_bler": attr(ue, "dl_bler"),

            "ul_sched_rb": attr(ue, "ul_sched_rb"),
            "dl_sched_rb": attr(ue, "dl_sched_rb"),

            "ul_aggr_prb": attr(ue, "ul_aggr_prb"),
            "dl_aggr_prb": attr(ue, "dl_aggr_prb"),

            "ul_retx_prb": attr(ue, "ul_aggr_retx_prb"),
            "dl_retx_prb": attr(ue, "dl_aggr_retx_prb"),

            "ul_curr_tbs": attr(ue, "ul_curr_tbs"),
            "dl_curr_tbs": attr(ue, "dl_curr_tbs"),

            "ul_aggr_tbs": attr(ue, "ul_aggr_tbs"),
            "dl_aggr_tbs": attr(ue, "dl_aggr_tbs"),

            "ul_aggr_sdu_bytes": attr(ue, "ul_aggr_bytes_sdus"),
            "dl_aggr_sdu_bytes": attr(ue, "dl_aggr_bytes_sdus"),

            "pusch_snr": attr(ue, "pusch_snr"),
            "pucch_snr": attr(ue, "pucch_snr"),
            "wb_cqi": attr(ue, "wb_cqi"),

            "bsr_raw": bsr_raw,
            "bsr_i32": uint32_to_int32(bsr_raw),
            "phr": attr(ue, "phr"),
        })

    return result


def extract_rlc(ind):
    result = []

    for rb in vector_items(attr(ind, "rb_stats", [])):
        result.append({
            "rnti": attr(rb, "rnti"),
            "rbid": attr(rb, "rbid"),
            "mode": attr(rb, "mode"),

            "txbuf_bytes": attr(rb, "txbuf_occ_bytes"),
            "txbuf_pkts": attr(rb, "txbuf_occ_pkts"),
            "rxbuf_bytes": attr(rb, "rxbuf_occ_bytes"),
            "rxbuf_pkts": attr(rb, "rxbuf_occ_pkts"),

            "txpdu_bytes": attr(rb, "txpdu_bytes"),
            "rxpdu_bytes": attr(rb, "rxpdu_bytes"),

            "txpdu_pkts": attr(rb, "txpdu_pkts"),
            "rxpdu_pkts": attr(rb, "rxpdu_pkts"),

            "retx_bytes": attr(rb, "txpdu_retx_bytes"),
            "retx_pkts": attr(rb, "txpdu_retx_pkts"),

            "txsdu_bytes": attr(rb, "txsdu_bytes"),
            "rxsdu_bytes": attr(rb, "rxsdu_bytes"),

            "txsdu_pkts": attr(rb, "txsdu_pkts"),
            "rxsdu_pkts": attr(rb, "rxsdu_pkts"),

            "txsdu_avg_time_to_tx": attr(
                rb,
                "txsdu_avg_time_to_tx",
            ),
            "txsdu_wt_us": attr(rb, "txsdu_wt_us"),
            "txpdu_wt_ms": attr(rb, "txpdu_wt_ms"),
        })

    return result


def extract_pdcp(ind):
    result = []

    for rb in vector_items(attr(ind, "rb_stats", [])):
        result.append({
            "rnti": attr(rb, "rnti"),
            "rbid": attr(rb, "rbid"),
            "mode": attr(rb, "mode"),

            "txpdu_bytes": attr(rb, "txpdu_bytes"),
            "rxpdu_bytes": attr(rb, "rxpdu_bytes"),

            "txpdu_pkts": attr(rb, "txpdu_pkts"),
            "rxpdu_pkts": attr(rb, "rxpdu_pkts"),

            "txsdu_bytes": attr(rb, "txsdu_bytes"),
            "rxsdu_bytes": attr(rb, "rxsdu_bytes"),

            "txsdu_pkts": attr(rb, "txsdu_pkts"),
            "rxsdu_pkts": attr(rb, "rxsdu_pkts"),

            "rx_oo_pkts": attr(rb, "rxpdu_oo_pkts"),
            "rx_dd_pkts": attr(rb, "rxpdu_dd_pkts"),

            "tx_sn": attr(rb, "txpdu_sn"),
            "rx_sn": attr(rb, "rxpdu_sn"),
        })

    return result


def extract_gtp(ind):
    result = []

    for tunnel in vector_items(attr(ind, "gtp_stats", [])):
        result.append({
            "rnti": attr(tunnel, "rnti"),
            "qfi": attr(tunnel, "qfi"),
            "teid_gnb": attr(tunnel, "teidgnb"),
            "teid_upf": attr(tunnel, "teidupf"),
        })

    return result


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

class MACCallback(ric.mac_cb):
    def __init__(self):
        ric.mac_cb.__init__(self)

    def handle(self, ind):
        values = extract_mac(ind)

        with state_lock:
            latest["mac"] = values
            indication_counts["mac"] += 1


class RLCCallback(ric.rlc_cb):
    def __init__(self):
        ric.rlc_cb.__init__(self)

    def handle(self, ind):
        values = extract_rlc(ind)

        with state_lock:
            latest["rlc"] = values
            indication_counts["rlc"] += 1


class PDCPCallback(ric.pdcp_cb):
    def __init__(self):
        ric.pdcp_cb.__init__(self)

    def handle(self, ind):
        values = extract_pdcp(ind)

        with state_lock:
            latest["pdcp"] = values
            indication_counts["pdcp"] += 1


class GTPCallback(ric.gtp_cb):
    def __init__(self):
        ric.gtp_cb.__init__(self)

    def handle(self, ind):
        values = extract_gtp(ind)

        with state_lock:
            latest["gtp"] = values
            indication_counts["gtp"] += 1


# ---------------------------------------------------------------------------
# Compact output
# ---------------------------------------------------------------------------

def print_snapshot():
    with state_lock:
        snapshot = {
            name: [dict(item) for item in values]
            for name, values in latest.items()
        }

        counts = dict(indication_counts)

    print()
    print("=" * 80)
    print(
        f"RAN TELEMETRY SNAPSHOT "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(
        "Indications: "
        f"MAC={counts['mac']} "
        f"RLC={counts['rlc']} "
        f"PDCP={counts['pdcp']} "
        f"GTP={counts['gtp']}"
    )
    print("-" * 80)

    #
    # MAC
    #
    if snapshot["mac"]:
        for ue in snapshot["mac"]:
            print(
                "MAC  "
                f"RNTI={ue['rnti']} "
                f"frame/slot={ue['frame']}/{ue['slot']} "
                f"MCS UL/DL={ue['ul_mcs']}/{ue['dl_mcs']} "
                f"BLER UL/DL={ue['ul_bler']:.3f}/{ue['dl_bler']:.3f} "
                f"SNR PUSCH/PUCCH={ue['pusch_snr']}/{ue['pucch_snr']} dB"
            )

            print(
                "     "
                f"PRB current UL/DL="
                f"{ue['ul_sched_rb']}/{ue['dl_sched_rb']} "
                f"PRB aggr UL/DL="
                f"{ue['ul_aggr_prb']}/{ue['dl_aggr_prb']} "
                f"PRB retx UL/DL="
                f"{ue['ul_retx_prb']}/{ue['dl_retx_prb']}"
            )

            print(
                "     "
                f"TBS current UL/DL="
                f"{ue['ul_curr_tbs']}/{ue['dl_curr_tbs']} "
                f"TBS aggr UL/DL="
                f"{ue['ul_aggr_tbs']}/{ue['dl_aggr_tbs']} "
                f"SDU bytes UL/DL="
                f"{ue['ul_aggr_sdu_bytes']}/{ue['dl_aggr_sdu_bytes']}"
            )

            print(
                "     "
                f"BSR raw/i32={ue['bsr_raw']}/{ue['bsr_i32']} "
                f"PHR={ue['phr']} "
                f"CQI={ue['wb_cqi']}"
            )
    else:
        print("MAC  no data")

    #
    # RLC
    #
    if snapshot["rlc"]:
        for rb in snapshot["rlc"]:
            print(
                "RLC  "
                f"RNTI={rb['rnti']} "
                f"RB={rb['rbid']} "
                f"mode={rb['mode']} "
                f"TXbuf={rb['txbuf_bytes']} B/{rb['txbuf_pkts']} pkts "
                f"RXbuf={rb['rxbuf_bytes']} B/{rb['rxbuf_pkts']} pkts"
            )

            print(
                "     "
                f"PDU bytes TX/RX="
                f"{rb['txpdu_bytes']}/{rb['rxpdu_bytes']} "
                f"SDU bytes TX/RX="
                f"{rb['txsdu_bytes']}/{rb['rxsdu_bytes']} "
                f"retx={rb['retx_bytes']} B/{rb['retx_pkts']} pkts"
            )

            print(
                "     "
                f"avg_tx_time={rb['txsdu_avg_time_to_tx']} "
                f"SDU_HOL={rb['txsdu_wt_us']} us "
                f"PDU_wait={rb['txpdu_wt_ms']} ms"
            )
    else:
        print("RLC  no data")

    #
    # PDCP
    #
    if snapshot["pdcp"]:
        for rb in snapshot["pdcp"]:
            print(
                "PDCP "
                f"RNTI={rb['rnti']} "
                f"RB={rb['rbid']} "
                f"mode={rb['mode']} "
                f"PDU bytes TX/RX="
                f"{rb['txpdu_bytes']}/{rb['rxpdu_bytes']} "
                f"SDU bytes TX/RX="
                f"{rb['txsdu_bytes']}/{rb['rxsdu_bytes']} "
                f"PDU pkts TX/RX="
                f"{rb['txpdu_pkts']}/{rb['rxpdu_pkts']}"
            )

            print(
                "     "
                f"out-of-order={rb['rx_oo_pkts']} "
                f"dropped/discarded={rb['rx_dd_pkts']} "
                f"SN TX/RX={rb['tx_sn']}/{rb['rx_sn']}"
            )
    else:
        print("PDCP no data")

    #
    # GTP
    #
    if snapshot["gtp"]:
        for tunnel in snapshot["gtp"]:
            print(
                "GTP  "
                f"RNTI={tunnel['rnti']} "
                f"QFI={tunnel['qfi']} "
                f"TEID gNB={tunnel['teid_gnb']} "
                f"TEID UPF={tunnel['teid_upf']}"
            )
    else:
        print("GTP  no data")

    print("=" * 80, flush=True)


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------

def get_interval():
    interval = getattr(ric, INTERVAL_NAME, None)

    if interval is None:
        available = sorted(
            name
            for name in dir(ric)
            if name.startswith("Interval_")
        )

        raise RuntimeError(
            f"xapp_sdk has no interval named {INTERVAL_NAME!r}. "
            f"Available intervals: {available}"
        )

    return interval


def subscribe_node(node, interval):
    subscriptions = (
        (
            "mac",
            MACCallback,
            ric.report_mac_sm,
        ),
        (
            "rlc",
            RLCCallback,
            ric.report_rlc_sm,
        ),
        (
            "pdcp",
            PDCPCallback,
            ric.report_pdcp_sm,
        ),
        (
            "gtp",
            GTPCallback,
            ric.report_gtp_sm,
        ),
    )

    print(f"Subscribing to E2 node: {node.id}")

    for sm_name, callback_class, report_function in subscriptions:
        try:
            cb = callback_class()

            # SWIG callback objects must remain alive.
            callbacks[sm_name].append(cb)

            handler = report_function(
                node.id,
                interval,
                cb,
            )

            handlers[sm_name].append(handler)

            print(
                f"  subscribed: {sm_name.upper()} "
                f"(handler={handler})"
            )

        except Exception as exc:
            print(
                f"  unable to subscribe to {sm_name.upper()}: {exc}",
                file=sys.stderr,
            )


def cleanup():
    global running

    running = False

    removal_functions = {
        "mac": ric.rm_report_mac_sm,
        "rlc": ric.rm_report_rlc_sm,
        "pdcp": ric.rm_report_pdcp_sm,
        "gtp": ric.rm_report_gtp_sm,
    }

    print("\nRemoving subscriptions...")

    for sm_name, sm_handlers in handlers.items():
        for handler in sm_handlers:
            try:
                removal_functions[sm_name](handler)
                print(
                    f"  removed {sm_name.upper()} "
                    f"subscription {handler}"
                )
            except Exception as exc:
                print(
                    f"  error removing {sm_name.upper()}: {exc}",
                    file=sys.stderr,
                )


def signal_handler(signum, frame):
    print(f"\nReceived signal {signum}")
    cleanup()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("Initializing FlexRIC xApp...")
    ric.init()

    interval = get_interval()
    print(f"Using reporting interval: {INTERVAL_NAME}")

    nodes = ric.conn_e2_nodes()
    print(f"Connected E2 nodes: {len(nodes)}")

    if len(nodes) == 0:
        raise RuntimeError(
            "No E2 nodes are currently connected to the nearRT-RIC"
        )

    for node in nodes:
        subscribe_node(node, interval)

    print()
    print("Subscriptions active.")
    print(
        f"Printing compact telemetry every "
        f"{PRINT_INTERVAL_S:.1f} second(s)."
    )
    print("Press Ctrl+C to stop.")

    while running:
        time.sleep(PRINT_INTERVAL_S)
        print_snapshot()


if __name__ == "__main__":
    main()