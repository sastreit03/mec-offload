#!/usr/bin/env python3
"""Prepare BDD videos that have matching semantic-segmentation annotations."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--dataset-output", type=Path, required=True)
    parser.add_argument("--video-output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=0, help="0 selects all matches")
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument(
        "--repo", default="dronefreak/BDD100K-Semantic-Segmentation"
    )
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    if not token:
        parser.error("HF_TOKEN is required")
    if not args.archive.is_file():
        parser.error(f"archive not found: {args.archive}")

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
    images = {
        Path(path).stem: path
        for path in files
        if Path(path).stem in masks
        and Path(path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    }
    with zipfile.ZipFile(args.archive) as source:
        video_members = {
            Path(member).stem: member
            for member in source.namelist()
            if Path(member).suffix.lower() in {".mov", ".mp4", ".mkv"}
        }

    matched = set(images) & set(masks) & set(video_members)
    selected = sorted(
        matched, key=lambda name: hashlib.sha256(name.encode()).hexdigest()
    )
    if args.count:
        selected = selected[: args.count]
    if not selected:
        raise RuntimeError("archive contains no videos with semantic annotations")

    print(f"archive videos: {len(video_members)}")
    print(f"video/annotation matches: {len(matched)}")
    print(f"selected matches: {len(selected)}")
    wanted = ["README.md", "id2label.json"]
    for name in selected:
        wanted.extend((images[name], masks[name]))
    args.dataset_output.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=args.repo,
        repo_type="dataset",
        token=token,
        local_dir=args.dataset_output,
        allow_patterns=wanted,
        max_workers=args.max_workers,
    )

    args.video_output.mkdir(parents=True, exist_ok=True)
    examples = []
    with zipfile.ZipFile(args.archive) as source:
        for name in selected:
            member = video_members[name]
            destination = args.video_output / (name + Path(member).suffix.lower())
            if not destination.is_file():
                # Copy through ZipFile.open so archive paths cannot escape the output directory.
                with source.open(member) as incoming, destination.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
            examples.append(
                {
                    "id": name,
                    "video": str(destination),
                    "archive_member": member,
                    "image": images[name],
                    "mask": masks[name],
                }
            )

    manifest = args.video_output.parent / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repository": args.repo,
                "archive": str(args.archive),
                "selection": "SHA-256 order of all matching training IDs",
                "examples": examples,
            },
            indent=2,
        )
    )
    print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()
