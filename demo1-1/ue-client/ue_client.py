#!/usr/bin/env python3
"""Minimal UE client for the OAI MEC YOLO Demo 1."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import cv2
import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager


class SourceAddressAdapter(HTTPAdapter):
    """Bind outgoing TCP connections to a specific local source IP."""

    def __init__(self, source_ip: str, **kwargs: Any) -> None:
        self.source_address = (source_ip, 0)
        super().__init__(**kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        pool_kwargs["source_address"] = self.source_address
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send an image to the MEC YOLO API through the UE PDU session."
    )
    parser.add_argument("--server", required=True)
    parser.add_argument("--source-ip", required=True)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("annotated.jpg"))
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.70)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--ntp", action='store_true')
    parser.add_argument("--result-json", type=Path)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not args.image.is_file():
        raise SystemExit(f"Input image does not exist: {args.image}")
    if args.count < 1:
        raise SystemExit("--count must be at least 1")
    if not 0.0 < args.conf <= 1.0:
        raise SystemExit("--conf must be in (0, 1]")
    if not 0.0 < args.iou <= 1.0:
        raise SystemExit("--iou must be in (0, 1]")
    if not 160 <= args.imgsz <= 1920:
        raise SystemExit("--imgsz must be between 160 and 1920")
    try:
        socket.inet_aton(args.source_ip)
    except OSError as exc:
        raise SystemExit(f"Invalid IPv4 source address: {args.source_ip}") from exc


def show_route(server_host: str, source_ip: str) -> None:
    result = subprocess.run(
        ["ip", "route", "get", server_host, "from", source_ip],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (result.stdout or result.stderr).strip()
    print(f"Route decision: {text}")
    if result.returncode != 0:
        raise SystemExit("Route lookup failed; verify the UE PDU session and source IP.")


def draw_detections(image_path: Path, payload: dict[str, Any], output_path: Path) -> None:
    image = cv2.imread(str(image_path))
    if image is None:
        raise RuntimeError(f"OpenCV could not decode {image_path}")

    height, width = image.shape[:2]

    for detection in payload.get("detections", []):
        box = detection.get("bbox_xyxy_norm", [])
        if len(box) != 4:
            continue

        x1 = max(0, min(width - 1, round(float(box[0]) * width)))
        y1 = max(0, min(height - 1, round(float(box[1]) * height)))
        x2 = max(0, min(width - 1, round(float(box[2]) * width)))
        y2 = max(0, min(height - 1, round(float(box[3]) * height)))

        class_name = str(detection.get("class_name", detection.get("class_id", "object")))
        confidence = float(detection.get("confidence", 0.0))
        label = f"{class_name} {confidence:.2f}"

        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(
            image,
            label,
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image):
        raise RuntimeError(f"Failed to write annotated image: {output_path}")


def main() -> int:
    args = parse_args()
    validate_args(args)

    server = args.server.rstrip("/")
    host = requests.utils.urlparse(server).hostname
    if not host:
        raise SystemExit(f"Invalid server URL: {args.server}")

    show_route(host, args.source_ip)

    session = requests.Session()
    adapter = SourceAddressAdapter(args.source_ip)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    all_results: list[dict[str, Any]] = []

    for index in range(args.count):
        task_id = f"demo1-1-{uuid.uuid4()}"
        started = time.perf_counter()

        with args.image.open("rb") as image_file:
            # Get start time of HTTP post transfer
            post_start_time_ns = time.time_ns()

            response = session.post(
                f"{server}/v1/detect",
                files={
                    "image": (
                        args.image.name,
                        image_file,
                        "image/jpeg",
                    )
                },
                data={
                    "task_id": task_id,
                    "conf": str(args.conf),
                    "iou": str(args.iou),
                    "imgsz": str(args.imgsz),
                },
                timeout=args.timeout,
            )

        # Log timings
        response_complete_time_ns = time.time_ns()
        client_total_ms = (time.perf_counter() - started) * 1000.0

        # Calculate response latency
        response_latency_ms = 0
        if args.ntp:
            response_start_time_ns = int(response.headers["x-response-start-time-ns"])
            response_latency_ms = (response_complete_time_ns - response_start_time_ns) / 1_000_000.0

        try:
            payload = response.json()
        except ValueError:
            print(f"HTTP {response.status_code}: {response.text}", file=sys.stderr)
            return 1

        if not response.ok:
            print(json.dumps(payload, indent=2), file=sys.stderr)
            return 1

        # Calculate HTTP post transmission time
        upload_latency_ms = 0
        if args.ntp:
            post_complete_time_ns = payload["transfer"]["post_complete_time_ns"]
            upload_latency_ms = (post_complete_time_ns - post_start_time_ns) / 1_000_000.0

        postprocess_ms = None

        # Perform and time postprocessing on image with the metadata from server
        if index == args.count - 1:
            postprocess_start_time_ns = time.perf_counter_ns()
            draw_detections(args.image, payload, args.output)
            postprocess_ms = (time.perf_counter_ns() - postprocess_start_time_ns) / 1_000_000.0

        record = {
            "request_index": index + 1,
            "client_total_ms": round(client_total_ms, 3),
            "upload_latency_ms": (
                round(upload_latency_ms, 3)
                if args.ntp
                else "NTP not used"
            ),
            "response_latency_ms": (
                round(response_latency_ms, 3)
                if args.ntp
                else "NTP not used"
            ),
            "postprocessing_ms": (
                round(postprocess_ms, 3)
                if postprocess_ms is not None
                else None
            ),
            "response": payload,
        }
        all_results.append(record)
        print(json.dumps(record, indent=2))

    if args.result_json:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(json.dumps(all_results, indent=2) + "\n")

    print(f"Annotated image: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
