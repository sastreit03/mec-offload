#!/usr/bin/env python3
"""Reconcile all transmitted frames with first complete-result receipt evidence."""

import argparse
import json
from pathlib import Path
import numpy as np


def evaluate(path):
    frames = {}
    attempts = 0
    suspensions = 0
    for line in Path(path).read_text().splitlines():
        m = json.loads(line)
        kind = m["type"]
        if kind == "admission_denied":
            attempts += 1
        if kind == "suspension":
            suspensions += 1
        if "video_id" not in m or "frame_id" not in m:
            continue
        key = (m.get("run_id"), m["video_id"], m["frame_id"])
        f = frames.setdefault(key, {})
        if kind == "transmitted":
            f["transmitted"] = m
        elif kind == "result_receipt" and "receipt" not in f:
            f["receipt"] = m
        elif kind == "deadline_miss":
            f["expired"] = True
    tx = [f for f in frames.values() if "transmitted" in f]
    receipts = [f["receipt"] for f in tx if "receipt" in f]
    for f in tx:
        r = f.get("receipt")
        if r and r.get("receipt_mono_ns") is not None:
            r["latency_ns"] = r["receipt_mono_ns"] - f["transmitted"]["tx_mono_ns"]
            r["slo_success"] = (
                0 <= r["latency_ns"] <= 150_000_000 and r["outcome"] == "completed"
            )
    successes = sum(bool(f.get("receipt", {}).get("slo_success")) for f in tx)
    lat = [m["latency_ns"] / 1e6 for m in receipts if m.get("latency_ns") is not None]
    return dict(
        transmitted_frames=len(tx),
        received_results=len(receipts),
        slo_success_frames=successes,
        slo_compliance=successes / len(tx) if tx else None,
        missing_results=len(tx) - len(receipts),
        received_latency_ms={
            f"p{p}": float(np.percentile(lat, p)) for p in [50, 95, 99]
        }
        if lat
        else {},
        denied_attempt_events=attempts,
        suspension_events=suspensions,
        denominator="all transmitted frames",
        finalization="collect through last deadline + 5 seconds; later arrivals are amendments",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("ue_log")
    p.add_argument("--output")
    a = p.parse_args()
    raw = json.dumps(evaluate(a.ue_log), indent=2)
    print(raw)
    if a.output:
        Path(a.output).write_text(raw + "\n")
