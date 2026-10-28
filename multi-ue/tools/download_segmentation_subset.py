#!/usr/bin/env python3
"""Download a deterministic paired BDD100K segmentation training subset."""

import argparse
import hashlib
import json
import os
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument(
        "--repo", default="dronefreak/BDD100K-Semantic-Segmentation"
    )
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    if not token:
        parser.error("HF_TOKEN is required")
    from huggingface_hub import HfApi, snapshot_download

    files = HfApi().list_repo_files(
        repo_id=args.repo, repo_type="dataset", token=token
    )
    masks = {
        Path(path).name.removesuffix("_train_id.png"): path
        for path in files
        if path.startswith("data/seg_maps/labels/train/")
        and path.endswith("_train_id.png")
    }
    # Match by example ID because the repository's image layout changed over time.
    images = {
        Path(path).stem: path
        for path in files
        if Path(path).stem in masks
        and Path(path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    }
    paired = set(images) & set(masks)
    print(f"repository training images: {len(images)}")
    print(f"repository training masks: {len(masks)}")
    print(f"paired training examples: {len(paired)}")
    if len(masks) != 7000 or len(paired) != 7000:
        examples = [path for path in files if "images_10k" in path][:10]
        raise RuntimeError(
            f"unexpected repository layout; example image paths: {examples}"
        )
    if args.count < 1 or args.count > len(paired):
        parser.error(f"--count must be between 1 and {len(paired)}")

    selected = sorted(
        paired, key=lambda name: hashlib.sha256(name.encode()).hexdigest()
    )[: args.count]
    wanted = ["README.md", "id2label.json"]
    for name in selected:
        wanted.extend((images[name], masks[name]))

    args.output.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=args.repo,
        repo_type="dataset",
        token=token,
        local_dir=args.output,
        allow_patterns=wanted,
        max_workers=args.max_workers,
    )
    (args.output / f"quality-subset-{args.count}.json").write_text(
        json.dumps(
            {
                "repository": args.repo,
                "selection": f"first {args.count} by SHA-256 of example ID",
                "examples": [
                    {"id": name, "image": images[name], "mask": masks[name]}
                    for name in selected
                ],
            },
            indent=2,
        )
    )
    print(f"Selected segmentation pairs: {len(selected)}")


if __name__ == "__main__":
    main()
