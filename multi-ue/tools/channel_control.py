#!/usr/bin/env python3
"""Small scenario-boundary client for the Aetheredge SRK CIR controller."""

import argparse
import json
from pathlib import Path

from channel_preflight import request
from channel_trace import validate_trace_folder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("config", "status", "select"))
    # The host reaches the gNB through its static oai-e2-net bridge address.
    parser.add_argument("--endpoint", default="tcp://192.168.73.140:5560")
    parser.add_argument("--trace-bank")
    parser.add_argument("--condition-id")
    args = parser.parse_args()

    if args.command == "config":
        payload = {"msg_type": "config_req"}
    elif args.command == "status":
        payload = {"msg_type": "trace_status"}
    else:
        if not args.trace_bank or not args.condition_id:
            parser.error("select requires --trace-bank and --condition-id")
        record = validate_trace_folder(
            Path(args.trace_bank) / args.condition_id,
            condition_id=args.condition_id,
        )
        payload = {
            "msg_type": "trace_select",
            "condition_id": args.condition_id,
            "trace_sha256": record["trace_sha256"],
        }
    print(json.dumps(request(args.endpoint, payload), indent=2))


if __name__ == "__main__":
    main()
