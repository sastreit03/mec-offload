#!/usr/bin/env python3
"""Prepare BDD videos with matching Hugging Face detection images and labels."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile


def object_labels(raw):
    """Return detection objects from either supported BDD JSON schema."""
    records = raw if isinstance(raw, list) else [raw]
    objects = []
    for record in records:
        if not isinstance(record, dict):
            continue
        # Current Scalabel-formatted BDD annotations store objects as labels.
        objects.extend(record.get("labels", []))
        objects.extend(record.get("objects", []))
        # The original 2018 release nests objects inside its frame records.
        for frame in record.get("frames", []):
            objects.extend(frame.get("labels", []))
            objects.extend(frame.get("objects", []))
        # Some per-image mirrors store the object list itself as the JSON array.
        if "category" in record and "box2d" in record:
            objects.append(record)
    return objects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--dataset-output", type=Path, required=True)
    parser.add_argument("--video-output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--repo", default="dronefreak/BDD100K")
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    if not token:
        parser.error("HF_TOKEN is required")
    if not args.archive.is_file():
        parser.error(f"archive not found: {args.archive}")
    if args.count <= 0:
        parser.error("count must be positive")

    from huggingface_hub import HfApi, snapshot_download

    files = HfApi().list_repo_files(
        repo_id=args.repo, repo_type="dataset", token=token
    )
    # The current mirror preserves BDD's raw per-image JSON annotations.
    image_paths = {
        Path(path).stem: path
        for path in files
        if path.startswith("data/images_100k/100k/train/")
        and path.lower().endswith(".jpg")
    }
    annotation_paths = {
        Path(path).stem: path
        for path in files
        if path.startswith("data/labels/100k/train/")
        and path.lower().endswith(".json")
    }
    if not image_paths or not annotation_paths:
        raise RuntimeError("repository has no raw BDD train images or annotations")
    with zipfile.ZipFile(args.archive) as source:
        videos = {
            Path(member).stem: member
            for member in source.namelist()
            if Path(member).suffix.lower() in {".mov", ".mp4", ".mkv"}
        }
    matched = set(image_paths) & set(annotation_paths) & set(videos)
    selected = sorted(
        matched, key=lambda name: hashlib.sha256(name.encode()).hexdigest()
    )[: args.count]
    if len(selected) < args.count:
        raise RuntimeError(
            f"requested {args.count} pairs but only {len(selected)} are available"
        )
    print(f"archive videos: {len(videos)}")
    print(f"video/detection matches: {len(matched)}")
    print(f"selected pairs: {len(selected)}")

    args.dataset_output.mkdir(parents=True, exist_ok=True)
    wanted = [
        path
        for sample_id in selected
        for path in (image_paths[sample_id], annotation_paths[sample_id])
    ]
    snapshot_download(
        repo_id=args.repo,
        repo_type="dataset",
        token=token,
        local_dir=args.dataset_output,
        allow_patterns=wanted,
        max_workers=args.max_workers,
    )

    # Preserve the class order used by BDD and the production quality profiler.
    names = [
        "person", "rider", "car", "truck", "bus", "train", "motor", "bike",
        "traffic light", "traffic sign",
    ]
    name_to_id = {name: index for index, name in enumerate(names)}
    data_directory = args.dataset_output / "data"
    data_directory.mkdir(parents=True, exist_ok=True)
    (data_directory / "data.yaml").write_text(
        "names:\n" + "".join(f"  {index}: {name}\n" for index, name in enumerate(names))
    )

    args.video_output.mkdir(parents=True, exist_ok=True)
    label_output = args.dataset_output / "selected-labels"
    label_output.mkdir(parents=True, exist_ok=True)
    examples = []
    with zipfile.ZipFile(args.archive) as source:
        for sample_id in selected:
            member = videos[sample_id]
            destination = args.video_output / (sample_id + Path(member).suffix.lower())
            if not destination.is_file():
                with source.open(member) as incoming, destination.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
            image_path = image_paths[sample_id]
            raw = json.loads(
                (args.dataset_output / annotation_paths[sample_id]).read_text()
            )
            boxes = []
            categories = []
            for label in object_labels(raw):
                category = label.get("category")
                box = label.get("box2d")
                if category not in name_to_id or not box:
                    continue
                x1, y1 = float(box["x1"]), float(box["y1"])
                x2, y2 = float(box["x2"]), float(box["y2"])
                boxes.append((x1, y1, x2 - x1, y2 - y1))
                categories.append(name_to_id[category])
            if not boxes:
                raise RuntimeError(f"no detection boxes for {sample_id}")
            label_path = label_output / f"{sample_id}.txt"
            with label_path.open("w") as output:
                for category, box in zip(categories, boxes):
                    x, y, width, height = map(float, box)
                    output.write(
                        f"{category} {(x + width / 2) / 1280:.10f} "
                        f"{(y + height / 2) / 720:.10f} {width / 1280:.10f} "
                        f"{height / 720:.10f}\n"
                    )
            examples.append({
                "id": sample_id,
                "video": str(destination),
                "archive_member": member,
                "image": image_path,
                "label": str(label_path.relative_to(args.dataset_output)),
            })
    manifest = args.video_output.parent / "manifest.json"
    manifest.write_text(json.dumps({
        "repository": args.repo,
        "archive": str(args.archive),
        "selection": "SHA-256 order of matching video/detection IDs",
        "examples": examples,
    }, indent=2))
    print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()
