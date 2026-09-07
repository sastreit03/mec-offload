#!/usr/bin/env python3

import os
import signal
import sys
import time
from collections import defaultdict
from typing import Any

import xapp_sdk as ric


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INTERVAL_NAME = os.getenv("XAPP_INTERVAL", "Interval_ms_10")
MAX_DUMPS_PER_SM = int(os.getenv("MAX_DUMPS_PER_SM", "5"))

MAX_DEPTH = 5
MAX_SEQUENCE_ITEMS = 50


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

indication_counts = defaultdict(int)
dump_counts = defaultdict(int)
running = True


# ---------------------------------------------------------------------------
# SWIG/introspection helpers
# ---------------------------------------------------------------------------

def is_scalar(value: Any) -> bool:
    return value is None or isinstance(
        value,
        (bool, int, float, str, bytes),
    )


def public_attributes(obj: Any) -> list[str]:
    """
    Return readable-looking public SWIG/Python attributes.

    Methods and private/internal attributes are filtered out later.
    """
    try:
        return [
            name
            for name in dir(obj)
            if not name.startswith("_")
        ]
    except Exception:
        return []


def dump_object(
    obj: Any,
    name: str = "object",
    depth: int = 0,
    seen: set[int] | None = None,
) -> None:
    """
    Recursively print a SWIG indication object.

    This is intentionally generic: we don't assume field names for MAC, RLC,
    PDCP, or GTP. The purpose is to discover what this exact xapp_sdk build
    exposes.
    """
    if seen is None:
        seen = set()

    indent = "  " * depth

    if depth > MAX_DEPTH:
        print(f"{indent}{name}: <maximum recursion depth reached>")
        return

    if is_scalar(obj):
        print(f"{indent}{name}: {obj!r}")
        return

    object_id = id(obj)
    if object_id in seen:
        print(f"{indent}{name}: <already visited>")
        return

    seen.add(object_id)

    # Python lists / tuples.
    if isinstance(obj, (list, tuple)):
        print(f"{indent}{name}: {type(obj).__name__}[{len(obj)}]")
        for index, value in enumerate(obj[:MAX_SEQUENCE_ITEMS]):
            dump_object(
                value,
                name=f"[{index}]",
                depth=depth + 1,
                seen=seen,
            )
        if len(obj) > MAX_SEQUENCE_ITEMS:
            print(
                f"{indent}  ... "
                f"{len(obj) - MAX_SEQUENCE_ITEMS} additional items omitted"
            )
        return

    # SWIG vectors generally support len() and indexing even though they are
    # not normal Python lists.
    if not isinstance(obj, (str, bytes)):
        try:
            length = len(obj)
        except Exception:
            length = None

        if length is not None:
            try:
                print(
                    f"{indent}{name}: "
                    f"{type(obj).__name__}[{length}]"
                )

                for index in range(min(length, MAX_SEQUENCE_ITEMS)):
                    dump_object(
                        obj[index],
                        name=f"[{index}]",
                        depth=depth + 1,
                        seen=seen,
                    )

                if length > MAX_SEQUENCE_ITEMS:
                    print(
                        f"{indent}  ... "
                        f"{length - MAX_SEQUENCE_ITEMS} additional items omitted"
                    )
                return
            except Exception:
                # It had len(), but wasn't safely indexable.
                pass

    attrs = public_attributes(obj)

    if not attrs:
        print(f"{indent}{name}: {obj!r}")
        return

    print(f"{indent}{name}: {type(obj).__name__}")

    for attr_name in attrs:
        try:
            value = getattr(obj, attr_name)
        except Exception as exc:
            print(
                f"{indent}  {attr_name}: "
                f"<unable to read: {exc}>"
            )
            continue

        # Don't invoke methods.
        if callable(value):
            continue

        dump_object(
            value,
            name=attr_name,
            depth=depth + 1,
            seen=seen,
        )


def print_indication(sm_name: str, ind: Any) -> None:
    indication_counts[sm_name] += 1

    count = indication_counts[sm_name]

    # Only produce a limited number of full object dumps per SM.
    if dump_counts[sm_name] >= MAX_DUMPS_PER_SM:
        if count % 1000 == 0:
            print(
                f"[{sm_name.upper()}] "
                f"received {count} total indications"
            )
        return

    dump_counts[sm_name] += 1

    print()
    print("=" * 80)
    print(
        f"{sm_name.upper()} INDICATION "
        f"#{count} "
        f"(dump {dump_counts[sm_name]}/{MAX_DUMPS_PER_SM})"
    )
    print("=" * 80)

    dump_object(ind, name="ind")

    print("=" * 80)
    print(flush=True)


# ---------------------------------------------------------------------------
# Service-model callbacks
# ---------------------------------------------------------------------------

class MACCallback(ric.mac_cb):
    def __init__(self):
        ric.mac_cb.__init__(self)

    def handle(self, ind):
        print_indication("mac", ind)


class RLCCallback(ric.rlc_cb):
    def __init__(self):
        ric.rlc_cb.__init__(self)

    def handle(self, ind):
        print_indication("rlc", ind)


class PDCPCallback(ric.pdcp_cb):
    def __init__(self):
        ric.pdcp_cb.__init__(self)

    def handle(self, ind):
        print_indication("pdcp", ind)


class GTPCallback(ric.gtp_cb):
    def __init__(self):
        ric.gtp_cb.__init__(self)

    def handle(self, ind):
        print_indication("gtp", ind)


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


def subscribe_node(node, interval) -> None:
    print(f"Subscribing to E2 node: {node.id}")

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

    for sm_name, callback_class, report_function in subscriptions:
        try:
            cb = callback_class()

            # Keeping the callback alive is essential with the SWIG bindings.
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


def cleanup() -> None:
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
        remove_function = removal_functions[sm_name]

        for handler in sm_handlers:
            try:
                remove_function(handler)
                print(
                    f"  removed {sm_name.upper()} "
                    f"subscription {handler}"
                )
            except Exception as exc:
                print(
                    f"  error removing {sm_name.upper()} "
                    f"subscription {handler}: {exc}",
                    file=sys.stderr,
                )

    # Some FlexRIC versions expose try_stop().
    if hasattr(ric, "try_stop"):
        try:
            while ric.try_stop() == 0:
                time.sleep(1)
        except Exception as exc:
            print(
                f"Warning while stopping xApp SDK: {exc}",
                file=sys.stderr,
            )


def signal_handler(signum, frame):
    print(f"\nReceived signal {signum}")
    cleanup()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("Initializing FlexRIC xApp...")
    ric.init()

    print("Python xapp_sdk customized SM interfaces:")
    for name in sorted(dir(ric)):
        if name.startswith("report_") and name.endswith("_sm"):
            print(f"  {name}")

    interval = get_interval()
    print(f"Using reporting interval: {INTERVAL_NAME}")

    nodes = ric.conn_e2_nodes()

    print(f"Connected E2 nodes: {len(nodes)}")

    if len(nodes) == 0:
        raise RuntimeError(
            "No E2 nodes are currently connected to the nearRT-RIC"
        )

    for node in nodes:
        print()
        print("E2 node:")
        dump_object(node, name="node")
        subscribe_node(node, interval)

    print()
    print("Subscriptions active.")
    print(
        f"Will print at most {MAX_DUMPS_PER_SM} "
        "full indications per service model."
    )
    print("Press Ctrl+C to stop.")

    while running:
        time.sleep(1)


if __name__ == "__main__":
    main()