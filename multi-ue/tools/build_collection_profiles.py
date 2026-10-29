#!/usr/bin/env python3
"""Build collection-mode profiles from the measured profiling artifacts."""

import argparse
import csv
import hashlib
import math
import statistics
from collections import defaultdict
from pathlib import Path

import yaml


def rows(path):
    with Path(path).open(newline="") as source:
        return list(csv.DictReader(source))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def positive(value, label):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="config/tasks.yaml")
    parser.add_argument("--processing", required=True)
    parser.add_argument("--frame-sizes", required=True)
    parser.add_argument("--detection-quality", required=True)
    parser.add_argument("--segmentation-quality", required=True)
    parser.add_argument("--priority", type=int, default=-3)
    parser.add_argument("--compatibility", default="measured-testbed-v1")
    parser.add_argument("--output", default="config/profiles.measured.yaml")
    args = parser.parse_args()

    catalog = yaml.safe_load(Path(args.catalog).read_text())
    candidates = catalog["configurations"]

    processing = defaultdict(list)
    tracker = defaultdict(list)
    for row in rows(args.processing):
        if int(row["priority"]) != args.priority:
            continue
        key = (row["application"], row["model"])
        processing[key].append(positive(row["processing_s"], "processing_s"))
        if row["tracker_s"]:
            tracker[key].append(positive(row["tracker_s"], "tracker_s"))

    frame_bytes = defaultdict(list)
    for row in rows(args.frame_sizes):
        frame_type = row["frame_type"]
        if frame_type not in ("I", "P"):
            raise ValueError(f"unsupported frame type: {frame_type}")
        frame_bytes[(int(row["bitrate_bps"]), frame_type)].append(
            positive(row["encoded_bytes"], "encoded_bytes")
        )

    quality = {}
    for application, path, score_key, metric, dataset in (
        (
            "detection",
            args.detection_quality,
            "map50_95",
            "mAP50-95",
            "BDD100K detection video-backed training subset",
        ),
        (
            "segmentation",
            args.segmentation_quality,
            "miou",
            "mIoU",
            "BDD100K segmentation video-backed training subset",
        ),
    ):
        source_rows = rows(path)
        references = {
            row["model"]: positive(row[score_key], score_key)
            for row in source_rows
            if int(row["bitrate_bps"]) == 0
        }
        for row in source_rows:
            bitrate = int(row["bitrate_bps"])
            if bitrate == 0:
                continue
            quality[(application, row["model"], bitrate)] = {
                "score": positive(row[score_key], score_key),
                "reference_score": references[row["model"]],
                "metric": metric,
                "dataset": dataset,
                "quality_examples": int(row["examples"]),
            }

    profiles = {}
    for candidate in candidates:
        cid = candidate["id"]
        application = candidate["application"]
        model = candidate["model"]
        bitrate = candidate["bitrate_bps"]
        timing_key = (application, model)
        if not processing[timing_key]:
            raise ValueError(f"missing processing measurements for {cid}")
        if application == "tracking" and not tracker[timing_key]:
            raise ValueError(f"missing tracker measurements for {cid}")
        for frame_type in ("I", "P"):
            if not frame_bytes[(bitrate, frame_type)]:
                raise ValueError(f"missing {frame_type}-frame measurements for {cid}")

        profile = {
            # Collection mode uses these measurements for worker initialization.
            "processing_s": statistics.median(processing[timing_key]),
            "frame_bytes_I": statistics.mean(frame_bytes[(bitrate, "I")]),
            "frame_bytes_P": statistics.mean(frame_bytes[(bitrate, "P")]),
            "processing_samples": len(processing[timing_key]),
            "frame_samples_I": len(frame_bytes[(bitrate, "I")]),
            "frame_samples_P": len(frame_bytes[(bitrate, "P")]),
            "processing_statistic": "median",
            "frame_size_statistic": "mean",
            "processing_priority": args.priority,
            # No live end-to-end residence profile exists for every candidate.
            "residence_initialization": "processing_s_until_10_live_completions",
            # Prevent this collection profile from enabling heuristic scheduling.
            "qualified": False,
        }
        if application == "tracking":
            profile["tracker_s"] = statistics.median(tracker[timing_key])
            profile["tracker_samples"] = len(tracker[timing_key])
            profile["quality_status"] = "not_measured_no_HOTA_ground_truth"
        else:
            key = (application, model, bitrate)
            if key not in quality:
                raise ValueError(f"missing quality measurement for {cid}")
            profile.update(quality[key])
        profiles[cid] = profile

    sources = {
        "catalog": args.catalog,
        "processing": args.processing,
        "frame_sizes": args.frame_sizes,
        "detection_quality": args.detection_quality,
        "segmentation_quality": args.segmentation_quality,
    }
    document = {
        "compatibility": args.compatibility,
        "synthetic": False,
        "purpose": "fixed-policy collection mode",
        "network": {
            "mean_s": None,
            "P0_s2": None,
            "R_s2": None,
            "q_s2_per_s": None,
            "V_frame_s2": None,
            "radio_bps_per_unit": None,
        },
        "provenance": {
            "sources": {
                name: {"path": str(path), "sha256": sha256(path)}
                for name, path in sources.items()
            },
            "notes": [
                "Network Kalman parameters remain unmeasured and unused in collection mode.",
                "Tracking quality remains unmeasured because no HOTA ground truth is available.",
            ],
        },
        "candidates": profiles,
    }
    output = Path(args.output)
    output.write_text(
        "# Generated by tools/build_collection_profiles.py from measured artifacts.\n"
        "# This profile initializes fixed-policy collection runs; it does not qualify\n"
        "# candidates for heuristic MEC scheduling.\n"
        + yaml.safe_dump(document, sort_keys=False)
    )
    print(f"wrote {len(profiles)} measured collection profiles to {output}")


if __name__ == "__main__":
    main()
