#!/usr/bin/env python3
"""Select, validate, and inventory the canonical campaign video set."""

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import yaml


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v"}


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def probe(path):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,nb_frames,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"no video stream in {path}")
    stream = streams[0]
    if stream.get("duration") in (None, "N/A"):
        stream["duration"] = json.loads(result.stdout).get("format", {}).get("duration")
    if stream.get("duration") in (None, "N/A"):
        raise RuntimeError(f"video duration is unavailable for {path}")
    frames = stream.get("nb_frames")
    if frames in (None, "N/A"):
        count = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        frames = count
    stream["source_frames"] = int(frames)
    return stream


def decode(path):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        check=True,
    )


def select_archive(archive, count, seed):
    with zipfile.ZipFile(archive) as source:
        members = sorted(
            name
            for name in source.namelist()
            if not name.endswith("/")
            and PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES
        )
    if len(members) < count:
        raise ValueError(f"archive contains {len(members)} videos; need {count}")
    return sorted(random.Random(seed).sample(members, count))


def stage_archive(archive, members, output):
    with zipfile.ZipFile(archive) as source:
        names = [PurePosixPath(member).name for member in members]
        if len(set(names)) != len(names):
            raise ValueError("selected archive videos have duplicate base names")
        for member, name in zip(members, names):
            target = output / name
            if target.exists():
                continue
            with source.open(member) as incoming, tempfile.NamedTemporaryFile(
                dir=output, delete=False
            ) as temporary:
                shutil.copyfileobj(incoming, temporary)
                temporary_path = Path(temporary.name)
            temporary_path.replace(target)
    return [output / PurePosixPath(member).name for member in members]


def stage_directory(source, count, seed, output):
    files = sorted(
        path
        for path in Path(source).rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )
    if len(files) < count:
        raise ValueError(f"source contains {len(files)} videos; need {count}")
    selected = sorted(random.Random(seed).sample(files, count))
    if len({path.name for path in selected}) != len(selected):
        raise ValueError("selected videos have duplicate base names")
    targets = []
    for path in selected:
        target = output / path.name
        if not target.exists():
            shutil.copy2(path, target)
        targets.append(target)
    return targets


def inventory(assets, android_prefix, linux_prefix):
    def entries(prefix):
        return [
            {
                "media_id": item["media_id"],
                "path": str(PurePosixPath(prefix) / item["filename"]),
                "sha256": item["sha256"],
                "source_frames": item["source_frames"],
            }
            for item in assets
        ]

    return {
        "schema_version": 1,
        "pools": {
            "pixel1": entries(android_prefix),
            "pixel2": entries(android_prefix),
            "pixel3": entries(android_prefix),
            "linux1": entries(linux_prefix),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--archive")
    sources.add_argument("--source-dir")
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=42)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--minimum-frames", type=int, default=1200)
    parser.add_argument("--decode-check", action="store_true")
    parser.add_argument("--inventory-output", required=True)
    parser.add_argument(
        "--android-prefix", default="/sdcard/Movies/AetheredgeCampaign"
    )
    parser.add_argument("--linux-prefix", default="/datasets/campaign")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.archive:
        selected = select_archive(args.archive, args.count, args.seed)
        paths = stage_archive(args.archive, selected, output)
        source = {"archive": str(args.archive), "members": selected}
    else:
        paths = stage_directory(args.source_dir, args.count, args.seed, output)
        source = {"directory": str(args.source_dir)}

    assets = []
    for number, path in enumerate(paths, 1):
        print(f"[{number}/{len(paths)}] validating {path.name}", flush=True)
        metadata = probe(path)
        if metadata["source_frames"] < args.minimum_frames:
            raise RuntimeError(
                f"{path} has {metadata['source_frames']} frames; "
                f"need {args.minimum_frames}"
            )
        if args.decode_check:
            decode(path)
        assets.append(
            {
                "media_id": path.stem,
                "filename": path.name,
                "sha256": sha256(path),
                "source_frames": metadata["source_frames"],
                "codec_name": metadata.get("codec_name"),
                "width": int(metadata["width"]),
                "height": int(metadata["height"]),
                "r_frame_rate": metadata.get("r_frame_rate"),
                "duration_s": float(metadata["duration"]),
                "selection_number": number,
            }
        )

    canonical = {
        "schema_version": 1,
        "selection": {
            "seed": args.seed,
            "count": args.count,
            **source,
        },
        "assets": assets,
    }
    (output / "canonical-media.yaml").write_text(
        yaml.safe_dump(canonical, sort_keys=False)
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{item['sha256']}  {item['filename']}\n" for item in assets)
    )
    inventory_path = Path(args.inventory_output)
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(
        yaml.safe_dump(
            inventory(assets, args.android_prefix, args.linux_prefix),
            sort_keys=False,
        )
    )
    print(f"validated {len(assets)} videos in {output}")
    print(f"wrote {inventory_path}")


if __name__ == "__main__":
    main()

