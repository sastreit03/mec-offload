#!/usr/bin/env python3
"""Validate SRK channel-emulation configuration and an Aetheredge trace bank."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

from channel_trace import validate_trace_folder
from generate_campaign_manifest import channel_conditions


def request(endpoint: str, payload: dict, timeout_ms: int = 5000) -> dict:
    try:
        import zmq
    except ImportError as exc:
        raise RuntimeError("pyzmq is required for online CIR preflight") from exc
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
    socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
    try:
        socket.connect(endpoint)
        socket.send_json(payload)
        response = socket.recv_json()
    finally:
        socket.close()
        context.term()
    if response.get("msg_type") == "error":
        raise RuntimeError(f"loader rejected request: {response}")
    return response


def validate_siso(path: Path):
    text = path.read_text()
    for field in ("nb_tx", "nb_rx"):
        values = [int(value) for value in re.findall(rf"\b{field}\s*=\s*(\d+)", text)]
        if not values or any(value != 1 for value in values):
            raise ValueError(f"{path}: {field} must be explicitly set to 1")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--trace-bank", required=True)
    parser.add_argument("--gnb-config")
    parser.add_argument("--compose")
    parser.add_argument("--endpoint", help="e.g. tcp://192.168.73.140:5560")
    parser.add_argument("--write-loader-config")
    parser.add_argument("--select-condition")
    args = parser.parse_args()

    campaign = yaml.safe_load(Path(args.campaign).read_text())
    if campaign["channels"]["scope"] != "common_bidirectional":
        raise ValueError("campaign channel scope must be common_bidirectional")
    expected = {
        condition["condition_id"]: condition for condition in channel_conditions(campaign)
    }
    if len(expected) != 111:
        raise ValueError(f"expected 111 channel conditions, found {len(expected)}")

    bank = Path(args.trace_bank)
    bank_manifest = json.loads((bank / "manifest.json").read_text())
    records = {}
    for condition_id in sorted(expected):
        records[condition_id] = validate_trace_folder(
            bank / condition_id, condition_id=condition_id
        )
    extras = {
        path.name for path in bank.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    } - set(expected)
    if extras:
        raise ValueError(f"unexpected trace folders: {sorted(extras)}")
    listed = {item["condition_id"]: item for item in bank_manifest["traces"]}
    if set(listed) != set(expected):
        raise ValueError("trace-bank manifest does not list exactly 111 conditions")
    for condition_id, record in records.items():
        if listed[condition_id]["trace_sha256"] != record["trace_sha256"]:
            raise ValueError(f"trace-bank manifest hash mismatch for {condition_id}")
        if record["reads_per_slot"] != int(campaign["channels"]["reads_per_slot"]):
            raise ValueError(f"unexpected reads_per_slot for {condition_id}")

    if args.gnb_config:
        validate_siso(Path(args.gnb_config))
    if args.compose:
        compose = Path(args.compose).read_text()
        if re.search(r"(?:^|\s|[-\"'])5556:5555(?:$|\s|[\"'])", compose, re.MULTILINE):
            raise ValueError("obsolete CIR mapping 5556:5555 remains in Compose file")
        # The controller is host-side, so it reaches the gNB on oai-e2-net.
        endpoint_host = urlparse(
            args.endpoint or "tcp://192.168.73.140:5560"
        ).hostname
        if endpoint_host and not re.search(
            rf"e2_net:\s*\n\s+ipv4_address:\s*{re.escape(endpoint_host)}\s*$",
            compose,
            re.MULTILINE,
        ):
            raise ValueError(
                f"Compose file does not assign CIR control host {endpoint_host} "
                "to the gNB e2_net interface"
            )
        if not re.search(r"channel_emulation/data/trace_bank[^\n]*:ro\s*$", compose, re.MULTILINE):
            raise ValueError("Compose file does not mount the trace bank read-only")

    online = None
    final_generation = None
    if args.endpoint:
        online = request(args.endpoint, {"msg_type": "config_req"})
        final_generation = online["generation"]
        if online.get("msg_type") != "config_res":
            raise RuntimeError(f"unexpected loader response: {online}")
        if int(online["control_port"]) != 5560:
            raise ValueError(f"loader uses unexpected port {online['control_port']}")
        radio = bank_manifest["radio"]
        comparisons = {
            "num_taps": "num_taps",
            "num_ofdm_symbols_per_slot": "num_symbols_per_slot",
            "fft_size": "fft_size",
        }
        for loader_field, manifest_field in comparisons.items():
            if int(online[loader_field]) != int(radio[manifest_field]):
                raise ValueError(
                    f"loader {loader_field}={online[loader_field]} differs from "
                    f"trace bank {radio[manifest_field]}"
                )
        for loader_field, manifest_field in (
            ("subcarrier_spacing", "subcarrier_spacing_hz"),
            ("frequency", "carrier_frequency_hz"),
        ):
            if abs(float(online[loader_field]) - float(radio[manifest_field])) > 1.0:
                raise ValueError(f"loader {loader_field} differs from trace bank")
        if args.write_loader_config:
            Path(args.write_loader_config).write_text(json.dumps(online, indent=2) + "\n")

        if args.select_condition:
            record = records[args.select_condition]
            ack = request(args.endpoint, {
                "msg_type": "trace_select",
                "condition_id": args.select_condition,
                "trace_sha256": record["trace_sha256"],
            })
            if ack.get("msg_type") != "trace_ack":
                raise RuntimeError(f"unexpected trace selection response: {ack}")
            status = request(args.endpoint, {"msg_type": "trace_status"})
            if status.get("condition_id") != args.select_condition:
                raise RuntimeError("loader did not retain selected condition")
            final_generation = status["generation"]
            print(json.dumps(status, indent=2))

    total_bytes = sum(record["bytes"] for record in records.values())
    print("Aetheredge channel preflight: PASS")
    print(f"conditions: {len(records)}")
    print(f"trace bytes: {total_bytes}")
    if online:
        print(f"loader: {args.endpoint} generation={final_generation}")


if __name__ == "__main__":
    main()
