#!/usr/bin/env python3
"""Measure YOLO mAP50-95 after production UE H.264 encoding."""

import argparse
import csv
import json
from pathlib import Path
import sys


# Merge BDD rider into COCO person. Generic traffic sign has no COCO equivalent.
BDD_TO_COCO = {0: 0, 1: 0, 2: 2, 3: 7, 4: 5, 5: 6, 6: 3, 7: 1, 8: 9}
CLASS_NAMES = {
    0: "person+BDD-rider", 1: "bicycle", 2: "car", 3: "motorcycle",
    5: "bus", 6: "train", 7: "truck", 9: "traffic light",
}


def box_iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def average_precision(records, class_id, threshold, np):
    ground_truth = {}
    total = 0
    predictions = []
    for image_id, record in enumerate(records):
        boxes = [box for cls, box in record["ground_truth"] if cls == class_id]
        ground_truth[image_id] = boxes
        total += len(boxes)
        predictions.extend(
            (score, image_id, box)
            for cls, score, box in record["predictions"] if cls == class_id
        )
    if total == 0:
        return None
    predictions.sort(reverse=True, key=lambda item: item[0])
    matched = {image_id: set() for image_id in ground_truth}
    true_positive, false_positive = [], []
    for _, image_id, prediction in predictions:
        candidates = ground_truth[image_id]
        overlaps = [box_iou(prediction, target) for target in candidates]
        best = max(range(len(overlaps)), key=overlaps.__getitem__) if overlaps else None
        success = (
            best is not None and overlaps[best] >= threshold
            and best not in matched[image_id]
        )
        true_positive.append(1 if success else 0)
        false_positive.append(0 if success else 1)
        if success:
            matched[image_id].add(best)
    if not predictions:
        return 0.0
    tp = np.cumsum(true_positive)
    fp = np.cumsum(false_positive)
    recall = tp / total
    precision = tp / np.maximum(tp + fp, 1)
    return float(np.mean([
        np.max(precision[recall >= point]) if np.any(recall >= point) else 0.0
        for point in np.linspace(0, 1, 101)
    ]))


def metrics(records, np):
    thresholds = [0.5 + 0.05 * index for index in range(10)]
    per_class = {}
    for class_id, name in CLASS_NAMES.items():
        values = [average_precision(records, class_id, threshold, np)
                  for threshold in thresholds]
        values = [value for value in values if value is not None]
        if values:
            per_class[name] = float(np.mean(values))
    return (
        float(np.mean(list(per_class.values()))),
        float(np.mean([
            value for class_id in CLASS_NAMES
            if (value := average_precision(records, class_id, 0.5, np)) is not None
        ])),
        per_class,
    )


def read_frame(video, target, cv2):
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"unreadable video: {video}")
    frame = None
    for _ in range(target + 1):
        ok, frame = capture.read()
        if not ok:
            capture.release()
            raise RuntimeError(f"video ended before frame {target}: {video}")
    capture.release()
    return frame


def ground_truth(path, width, height):
    boxes = []
    for line in path.read_text().splitlines():
        values = line.split()
        if len(values) != 5:
            raise RuntimeError(f"invalid YOLO label: {path}")
        bdd_class = int(values[0])
        if bdd_class not in BDD_TO_COCO:
            continue
        cx, cy, w, h = map(float, values[1:])
        boxes.append((BDD_TO_COCO[bdd_class], [
            (cx - w / 2) * width, (cy - h / 2) * height,
            (cx + w / 2) * width, (cy + h / 2) * height,
        ]))
    return boxes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("config/tasks.yaml"))
    parser.add_argument("--weights", type=Path, default=Path("weights"))
    parser.add_argument("--bitrate", type=int, action="append", required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--gop-frames", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0, help="0 profiles the manifest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import cv2
    import numpy as np
    import yaml
    from mec_contracts.rtp import canonical_au
    from mec_server.workers import TorchAdapter
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from profile_segmentation_quality import Decoder
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ue" / "linux" / "src"))
    from mec_ue.media import Encoder

    dataset_config = yaml.safe_load((args.dataset_root / "data/data.yaml").read_text())
    names = dataset_config["names"]
    if isinstance(names, dict):
        names = [names[index] if index in names else names[str(index)] for index in range(10)]
    expected_names = [
        "person", "rider", "car", "truck", "bus", "train", "motor", "bike",
        "traffic light", "traffic sign",
    ]
    if list(names) != expected_names:
        raise RuntimeError(f"unexpected BDD detection class order: {names}")

    catalog = yaml.safe_load(args.catalog.read_text())
    specs = {}
    for configuration in catalog["configurations"]:
        if configuration["application"] == "detection":
            specs.setdefault(configuration["model"], configuration)
    if len(specs) != 4:
        raise RuntimeError("expected four detection models")
    adapter = TorchAdapter(specs, args.weights, [0])
    examples = json.loads(args.manifest.read_text())["examples"]
    if args.limit < 0:
        parser.error("limit cannot be negative")
    if args.limit:
        examples = examples[: args.limit]
    bitrates = sorted(set(args.bitrate))
    conditions = [0] + bitrates
    records = {(model, bitrate): [] for model in specs for bitrate in conditions}

    for number, example in enumerate(examples, 1):
        if example["display_rotation"] != "none":
            raise RuntimeError("detection profiling requires normal display orientation")
        target = int(example["source_frame_id"])
        source_bgr = read_frame(example["video"], target, cv2)
        source_bgr = cv2.resize(source_bgr, (1280, 720))
        source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)
        truth = ground_truth(args.dataset_root / example["label"], 1280, 720)

        for model, configuration in specs.items():
            payload, _ = adapter.infer({
                "priority": 0, "configuration": configuration, "rgb": source_rgb,
            })
            predictions = sorted([
                (d["class_id"], d["score"], d["box"])
                for d in payload["detections"] if d["class_id"] in CLASS_NAMES
            ], key=lambda item: item[1], reverse=True)[:100]
            records[(model, 0)].append({
                "ground_truth": truth, "predictions": predictions,
            })

        for bitrate in bitrates:
            capture = cv2.VideoCapture(str(example["video"]))
            encoder = Encoder(1280, 720, args.fps, bitrate)
            decoder = Decoder(args.fps, target, np)
            try:
                for frame_id in range(target + 1):
                    ok, bgr = capture.read()
                    if not ok:
                        raise RuntimeError(f"video ended before target: {example['id']}")
                    bgr = cv2.resize(bgr, (1280, 720))
                    nals = encoder.frame(
                        bgr, frame_id, args.fps,
                        keyframe=frame_id % args.gop_frames == 0,
                        bitrate=bitrate,
                    )
                    decoder.frame(canonical_au(nals), frame_id)
                decoded_rgb = decoder.finish()
            finally:
                decoder.close()
                encoder.close()
                capture.release()
            for model, configuration in specs.items():
                payload, _ = adapter.infer({
                    "priority": 0, "configuration": configuration,
                    "rgb": decoded_rgb,
                })
                predictions = sorted([
                    (d["class_id"], d["score"], d["box"])
                    for d in payload["detections"] if d["class_id"] in CLASS_NAMES
                ], key=lambda item: item[1], reverse=True)[:100]
                records[(model, bitrate)].append({
                    "ground_truth": truth, "predictions": predictions,
                })
        print(f"completed {number}/{len(examples)}: {example['id']}", flush=True)

    args.output.mkdir(parents=True, exist_ok=True)
    summary, class_rows = [], []
    for model in specs:
        for bitrate in conditions:
            map5095, map50, per_class = metrics(records[(model, bitrate)], np)
            summary.append({
                "model": model, "bitrate_bps": bitrate,
                "map50_95": map5095, "map50": map50,
                "classes": len(per_class), "examples": len(examples),
            })
            for name, score in per_class.items():
                class_rows.append({
                    "model": model, "bitrate_bps": bitrate,
                    "class": name, "ap50_95": score,
                })
    for name, rows in (("summary.csv", summary), ("per-class.csv", class_rows)):
        with (args.output / name).open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    (args.output / "profile.json").write_text(json.dumps({
        "manifest": str(args.manifest), "dataset_root": str(args.dataset_root),
        "bitrates_bps": bitrates, "fps": args.fps, "gop_frames": args.gop_frames,
        "metric": "mAP50-95", "max_detections": 100,
        "iou_thresholds": [0.5 + 0.05 * index for index in range(10)],
        "class_mapping": {str(key): value for key, value in BDD_TO_COCO.items()},
        "excluded_bdd_classes": {"9": "traffic sign has no generic COCO class"},
    }, indent=2))


if __name__ == "__main__":
    main()
