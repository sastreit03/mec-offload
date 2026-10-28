#!/usr/bin/env python3
"""Measure SegFormer references on annotations and matched raw video frames."""

import argparse
import csv
import json
from pathlib import Path


def decode_rle(runs, height, width, np):
    flat = np.empty(height * width, dtype=np.int32)
    offset = 0
    for value, count in runs:
        flat[offset : offset + count] = value
        offset += count
    if offset != flat.size:
        raise RuntimeError("segmentation RLE length mismatch")
    return flat.reshape(height, width)


def add_confusion(confusion, truth, prediction, np):
    valid = truth != 255
    gt = truth[valid].astype(np.int64)
    pred = prediction[valid].astype(np.int64)
    confusion += np.bincount(gt * 19 + pred, minlength=361).reshape(19, 19)


def miou(confusion, np):
    intersection = np.diag(confusion).astype(np.float64)
    union = confusion.sum(0) + confusion.sum(1) - intersection
    present = union > 0
    return float(np.mean(intersection[present] / union[present])), int(present.sum())


def video_frame(path, target, cv2):
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"unreadable video: {path}")
    frame = None
    for _ in range(target + 1):
        ok, frame = capture.read()
        if not ok:
            capture.release()
            raise RuntimeError(f"video ended before target frame: {path}")
    capture.release()
    return frame


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("config/tasks.yaml"))
    parser.add_argument("--weights", type=Path, default=Path("weights"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import cv2
    import numpy as np
    from PIL import Image
    import yaml
    from mec_server.workers import TorchAdapter

    catalog = yaml.safe_load(args.catalog.read_text())
    specs = {}
    for configuration in catalog["configurations"]:
        if configuration["application"] == "segmentation":
            specs.setdefault(configuration["model"], configuration)
    if len(specs) != 4:
        raise RuntimeError("expected four segmentation models")
    adapter = TorchAdapter(specs, args.weights, [0])
    examples = json.loads(args.manifest.read_text())["examples"]
    conditions = ("annotation_jpeg", "source_video")
    counts = {
        (model, condition): np.zeros((19, 19), dtype=np.int64)
        for model in specs for condition in conditions
    }
    per_example = []

    for number, example in enumerate(examples, 1):
        truth = np.asarray(Image.open(args.dataset_root / example["mask"]), dtype=np.uint8)
        annotation = cv2.imread(str(args.dataset_root / example["image"]))
        source = video_frame(example["video"], int(example["source_frame_id"]), cv2)
        if example["display_rotation"] != "none":
            raise RuntimeError("reference profiler currently requires normal orientation")
        source = cv2.resize(source, (annotation.shape[1], annotation.shape[0]))
        images = {
            "annotation_jpeg": cv2.cvtColor(annotation, cv2.COLOR_BGR2RGB),
            "source_video": cv2.cvtColor(source, cv2.COLOR_BGR2RGB),
        }
        for model, configuration in specs.items():
            for condition, rgb in images.items():
                payload, _ = adapter.infer({
                    "priority": 0, "configuration": configuration, "rgb": rgb,
                })
                prediction = decode_rle(
                    payload["runs"], payload["height"], payload["width"], np
                )
                add_confusion(counts[(model, condition)], truth, prediction, np)
                image_counts = np.zeros((19, 19), dtype=np.int64)
                add_confusion(image_counts, truth, prediction, np)
                score, classes = miou(image_counts, np)
                per_example.append({
                    "id": example["id"], "model": model, "input": condition,
                    "miou": score, "classes": classes,
                })
        print(f"completed {number}/{len(examples)}: {example['id']}", flush=True)

    summary = []
    for model in specs:
        for condition in conditions:
            score, classes = miou(counts[(model, condition)], np)
            summary.append({
                "model": model, "input": condition, "miou": score,
                "classes": classes, "examples": len(examples),
            })
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in (("summary.csv", summary), ("per-example.csv", per_example)):
        with (args.output / name).open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    (args.output / "profile.json").write_text(json.dumps({
        "manifest": str(args.manifest), "dataset_root": str(args.dataset_root),
        "catalog": str(args.catalog), "weights": str(args.weights),
        "conditions": conditions, "metric": "mIoU", "ignore_class": 255,
    }, indent=2))


if __name__ == "__main__":
    main()
