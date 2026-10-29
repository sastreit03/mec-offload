#!/usr/bin/env python3
"""Measure production SegFormer mIoU on official Cityscapes fine labels."""

import argparse
import csv
import json
from pathlib import Path


# Official Cityscapes label IDs mapped to the 19 evaluation train IDs.
LABEL_TO_TRAIN = {
    7: 0, 8: 1, 11: 2, 12: 3, 13: 4, 17: 5, 19: 6, 20: 7,
    21: 8, 22: 9, 23: 10, 24: 11, 25: 12, 26: 13, 27: 14,
    28: 15, 31: 16, 32: 17, 33: 18,
}


def decode_rle(runs, height, width, np):
    flat = np.empty(height * width, dtype=np.int32)
    offset = 0
    for value, count in runs:
        flat[offset : offset + count] = value
        offset += count
    if offset != flat.size:
        raise RuntimeError("segmentation RLE length mismatch")
    return flat.reshape(height, width)


def train_ids(mask, already_train_ids, np):
    if already_train_ids:
        output = mask.astype(np.uint8)
        if not set(map(int, np.unique(output))).issubset(set(range(19)) | {255}):
            raise RuntimeError("unexpected Cityscapes train ID")
        return output
    output = np.full(mask.shape, 255, dtype=np.uint8)
    for label_id, train_id in LABEL_TO_TRAIN.items():
        output[mask == label_id] = train_id
    return output


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--split", default="val")
    parser.add_argument("--limit", type=int, default=0, help="0 evaluates the full split")
    parser.add_argument("--catalog", type=Path, default=Path("config/tasks.yaml"))
    parser.add_argument("--weights", type=Path, default=Path("weights"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import cv2
    import numpy as np
    import yaml
    from mec_server.workers import TorchAdapter

    images = sorted(
        (args.dataset_root / "leftImg8bit" / args.split).glob("*/*_leftImg8bit.png")
    )
    if not images:
        parser.error("no Cityscapes images found under leftImg8bit/<split>")
    if args.limit < 0:
        parser.error("limit cannot be negative")
    if args.limit:
        images = images[: args.limit]

    pairs = []
    for image in images:
        stem = image.name.removesuffix("_leftImg8bit.png")
        label_dir = args.dataset_root / "gtFine" / args.split / image.parent.name
        train_mask = label_dir / f"{stem}_gtFine_labelTrainIds.png"
        label_mask = label_dir / f"{stem}_gtFine_labelIds.png"
        if train_mask.is_file():
            pairs.append((image, train_mask, True))
        elif label_mask.is_file():
            pairs.append((image, label_mask, False))
        else:
            raise FileNotFoundError(f"no fine semantic label for {image}")

    catalog = yaml.safe_load(args.catalog.read_text())
    specs = {}
    for configuration in catalog["configurations"]:
        if configuration["application"] == "segmentation":
            specs.setdefault(configuration["model"], configuration)
    if len(specs) != 4:
        raise RuntimeError("expected four segmentation models")
    adapter = TorchAdapter(specs, args.weights, [0])
    counts = {model: np.zeros((19, 19), dtype=np.int64) for model in specs}
    per_image = []

    for number, (image_path, mask_path, is_train_ids) in enumerate(pairs, 1):
        bgr = cv2.imread(str(image_path))
        mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
        if bgr is None or mask is None or mask.shape != bgr.shape[:2]:
            raise RuntimeError(f"invalid image/mask pair: {image_path}")
        truth = train_ids(mask, is_train_ids, np)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        for model, configuration in specs.items():
            payload, _ = adapter.infer({
                "priority": 0, "configuration": configuration, "rgb": rgb,
            })
            prediction = decode_rle(
                payload["runs"], payload["height"], payload["width"], np
            )
            add_confusion(counts[model], truth, prediction, np)
            image_counts = np.zeros((19, 19), dtype=np.int64)
            add_confusion(image_counts, truth, prediction, np)
            score, classes = miou(image_counts, np)
            per_image.append({
                "image": str(image_path.relative_to(args.dataset_root)),
                "model": model, "miou": score, "classes": classes,
            })
        print(f"completed {number}/{len(pairs)}: {image_path.name}", flush=True)

    summary = []
    args.output.mkdir(parents=True, exist_ok=True)
    for model in specs:
        score, classes = miou(counts[model], np)
        summary.append({
            "model": model, "miou": score, "classes": classes,
            "images": len(pairs), "split": args.split,
        })
        np.save(args.output / f"confusion-{model}.npy", counts[model])
    for name, rows in (("summary.csv", summary), ("per-image.csv", per_image)):
        with (args.output / name).open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    (args.output / "profile.json").write_text(json.dumps({
        "dataset_root": str(args.dataset_root), "split": args.split,
        "images": len(pairs), "catalog": str(args.catalog),
        "weights": str(args.weights), "metric": "mIoU",
        "ignore_class": 255, "preprocessing": "production TorchAdapter",
    }, indent=2))


if __name__ == "__main__":
    main()
