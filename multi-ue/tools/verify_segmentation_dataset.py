#!/usr/bin/env python3
"""Verify BDD semantic labels and their correspondence to source-video frames."""

import argparse
import csv
import json
import math
from pathlib import Path


EXPECTED_LABELS = [
    "road", "sidewalk", "building", "wall", "fence", "pole",
    "traffic light", "traffic sign", "vegetation", "terrain", "sky",
    "person", "rider", "car", "truck", "bus", "train", "motorcycle",
    "bicycle",
]


def label_map(path):
    value = json.loads(path.read_text())
    if "id2label" in value:
        value = value["id2label"]
    if isinstance(value, list):
        value = dict(enumerate(value))
    return {int(key): str(name) for key, name in value.items()}


def normalized(name):
    return " ".join(name.lower().replace("_", " ").replace("-", " ").split())


def read_video_frame(video, target, cv2):
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"unreadable video: {video}")
    frame = None
    for frame_id in range(target + 1):
        ok, frame = capture.read()
        if not ok:
            capture.release()
            raise RuntimeError(f"video ended before frame {target}: {video}")
    capture.release()
    return frame


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import cv2
    import numpy as np

    dataset_labels = label_map(args.dataset_root / "id2label.json")
    maps = {"dataset": dataset_labels}
    for model in ("segformer-b0", "segformer-b1", "segformer-b2", "segformer-b3"):
        maps[model] = label_map(args.checkpoint_root / model / "config.json")
    for source, labels in maps.items():
        expected_ids = set(range(19)) | ({255} if source == "dataset" else set())
        if set(labels) != expected_ids:
            raise RuntimeError(
                f"{source} does not define the expected IDs {sorted(expected_ids)}"
            )
        if [normalized(labels[i]) for i in range(19)] != EXPECTED_LABELS:
            raise RuntimeError(f"{source} label order differs from Cityscapes-19")
    if normalized(dataset_labels[255]) not in {"unlabeled/ignore", "unlabeled ignore"}:
        raise RuntimeError("dataset ID 255 is not identified as the ignore class")

    manifest = json.loads(args.manifest.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    thumbnails = []
    class_pixels = np.zeros(19, dtype=np.int64)
    palette = np.array([
        [128, 64, 128], [244, 35, 232], [70, 70, 70], [102, 102, 156],
        [190, 153, 153], [153, 153, 153], [250, 170, 30], [220, 220, 0],
        [107, 142, 35], [152, 251, 152], [70, 130, 180], [220, 20, 60],
        [255, 0, 0], [0, 0, 142], [0, 0, 70], [0, 60, 100],
        [0, 80, 100], [0, 0, 230], [119, 11, 32],
    ], dtype=np.uint8)

    for example in manifest["examples"]:
        sample_id = example["id"]
        image_path = args.dataset_root / example["image"]
        mask_path = args.dataset_root / example["mask"]
        if image_path.stem != sample_id:
            raise RuntimeError(f"image ID mismatch for {sample_id}")
        if mask_path.name.removesuffix("_train_id.png") != sample_id:
            raise RuntimeError(f"mask ID mismatch for {sample_id}")
        image = cv2.imread(str(image_path))
        mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
        if image is None or mask is None or mask.shape != image.shape[:2]:
            raise RuntimeError(f"image/mask dimensions differ for {sample_id}")
        values, counts = np.unique(mask, return_counts=True)
        if not set(map(int, values)).issubset(set(range(19)) | {255}):
            raise RuntimeError(f"invalid mask class for {sample_id}")
        for value, count in zip(values, counts):
            if value != 255:
                class_pixels[int(value)] += int(count)

        frame = read_video_frame(example["video"], int(example["source_frame_id"]), cv2)
        if example["display_rotation"] == "clockwise":
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif example["display_rotation"] == "counterclockwise":
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif example["display_rotation"] == "resized":
            frame = cv2.resize(frame, (image.shape[1], image.shape[0]))
        elif example["display_rotation"] != "none":
            raise RuntimeError(f"unknown rotation for {sample_id}")
        if frame.shape != image.shape:
            raise RuntimeError(f"video/image dimensions differ for {sample_id}")
        difference = frame.astype(np.float32) - image.astype(np.float32)
        color_mae = float(np.mean(np.abs(difference)))
        mse = float(np.mean(difference * difference))
        psnr = math.inf if mse == 0 else 10 * math.log10(255 * 255 / mse)
        rows.append({
            "id": sample_id,
            "source_frame_id": int(example["source_frame_id"]),
            "match_mae_luma_320x180": float(example["match_mae"]),
            "distinct_margin": float(example["match_margin"]),
            "color_mae_full_resolution": color_mae,
            "psnr_db": psnr,
            "valid_pixels": int(np.count_nonzero(mask != 255)),
        })

        color_mask = np.zeros_like(image)
        valid = mask != 255
        color_mask[valid] = palette[mask[valid]][:, ::-1]
        overlay = image.copy()
        overlay[valid] = cv2.addWeighted(image[valid], 0.55, color_mask[valid], 0.45, 0)
        thumb = cv2.resize(overlay, (320, 180))
        cv2.putText(thumb, sample_id, (5, 17), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, (255, 255, 255), 1, cv2.LINE_AA)
        thumbnails.append(thumb)

    with (args.output / "correspondence.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    with (args.output / "label-mapping.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["class_id", *maps])
        for class_id in range(19):
            writer.writerow([class_id, *(maps[source][class_id] for source in maps)])
    columns = 4
    blank = np.zeros_like(thumbnails[0])
    contact_rows = []
    for offset in range(0, len(thumbnails), columns):
        contact_rows.append(np.hstack(
            thumbnails[offset : offset + columns]
            + [blank] * max(0, columns - len(thumbnails[offset : offset + columns]))
        ))
    cv2.imwrite(str(args.output / "mask-overlays.jpg"), np.vstack(contact_rows))
    (args.output / "report.json").write_text(json.dumps({
        "examples": len(rows),
        "labels": EXPECTED_LABELS,
        "ignore_label": {"id": 255, "name": dataset_labels[255]},
        "class_pixels": {EXPECTED_LABELS[i]: int(class_pixels[i]) for i in range(19)},
        "color_mae": {
            "minimum": min(row["color_mae_full_resolution"] for row in rows),
            "maximum": max(row["color_mae_full_resolution"] for row in rows),
            "mean": sum(row["color_mae_full_resolution"] for row in rows) / len(rows),
        },
        "status": "pass",
    }, indent=2))
    print(f"verified examples: {len(rows)}")
    print("label maps: dataset and four checkpoints match Cityscapes-19")
    print(f"mask overlay: {args.output / 'mask-overlays.jpg'}")


if __name__ == "__main__":
    main()
