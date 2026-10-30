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


def archive_candidates(archive, count, seed):
    with zipfile.ZipFile(archive) as source:
        members = sorted(
            name
            for name in source.namelist()
            if not name.endswith("/")
            and PurePosixPath(name).suffix.lower() in VIDEO_SUFFIXES
        )
    if len(members) < count:
        raise ValueError(f"archive contains {len(members)} videos; need {count}")
    random.Random(seed).shuffle(members)
    return members


def stage_archive_member(archive, member, output):
    with zipfile.ZipFile(archive) as source:
        target = output / PurePosixPath(member).name
        with source.open(member) as incoming, tempfile.NamedTemporaryFile(
            dir=output, delete=False
        ) as temporary:
            shutil.copyfileobj(incoming, temporary)
            temporary_path = Path(temporary.name)
        temporary_path.replace(target)
    return target


def directory_candidates(source, count, seed):
    files = sorted(
        path
        for path in Path(source).rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )
    if len(files) < count:
        raise ValueError(f"source contains {len(files)} videos; need {count}")
    random.Random(seed).shuffle(files)
    return files


def validate_random_access(path, frames, segments):
    """Exercise the exact seek-and-read pattern used by campaign UEs."""
    import cv2

    failures = []
    for segment in range(segments):
        start = segment * frames
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                failures.append(f"start={start}:open_failed")
                continue
            accepted = capture.set(cv2.CAP_PROP_POS_FRAMES, start)
            actual = int(capture.get(cv2.CAP_PROP_POS_FRAMES))
            if not accepted:
                failures.append(f"start={start}:seek_rejected")
                continue
            if actual != start:
                failures.append(f"start={start}:seek_mismatch:{actual}")
                continue
            decoded = 0
            for _ in range(frames):
                ok, _frame = capture.read()
                if not ok:
                    break
                decoded += 1
            if decoded != frames:
                failures.append(f"start={start}:decode_incomplete:{decoded}")
        finally:
            capture.release()
    if failures:
        raise RuntimeError("random access failed: " + ", ".join(failures))


def stage_directory_member(source, output):
    target = output / source.name
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as temporary:
        with source.open("rb") as incoming:
            shutil.copyfileobj(incoming, temporary)
        temporary_path = Path(temporary.name)
    temporary_path.replace(target)
    return target


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
    parser.add_argument(
        "--exclude-media-id",
        action="append",
        default=[],
        help="media stem to skip; may be supplied more than once",
    )
    parser.add_argument(
        "--random-access-check",
        action="store_true",
        help="validate every campaign segment using OpenCV random access",
    )
    parser.add_argument("--segment-frames", type=int, default=150)
    parser.add_argument("--segments-per-video", type=int, default=8)
    parser.add_argument("--inventory-output", required=True)
    parser.add_argument(
        "--android-prefix", default="/sdcard/Movies/AetheredgeCampaign"
    )
    parser.add_argument("--linux-prefix", default="/datasets/campaign")
    args = parser.parse_args()

    if args.segment_frames <= 0 or args.segments_per_video <= 0:
        parser.error("segment-frames and segments-per-video must be positive")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.archive:
        candidates = archive_candidates(args.archive, args.count, args.seed)
        materialize = lambda candidate: stage_archive_member(
            args.archive, candidate, output
        )
    else:
        candidates = directory_candidates(args.source_dir, args.count, args.seed)
        materialize = lambda candidate: stage_directory_member(candidate, output)

    assets = []
    selected_sources = []
    excluded_media_ids = set(args.exclude_media_id)
    for candidate_number, candidate in enumerate(candidates, 1):
        if PurePosixPath(str(candidate)).stem in excluded_media_ids:
            print(f"SKIP {candidate}: explicitly excluded", flush=True)
            continue
        path = materialize(candidate)
        print(
            f"[{len(assets) + 1}/{args.count}] validating {path.name} "
            f"(candidate {candidate_number})",
            flush=True,
        )
        try:
            metadata = probe(path)
            if metadata["source_frames"] < args.minimum_frames:
                raise RuntimeError(
                    f"only {metadata['source_frames']} frames; "
                    f"need {args.minimum_frames}"
                )
            if args.decode_check:
                decode(path)
            if args.random_access_check:
                validate_random_access(
                    path,
                    args.segment_frames,
                    args.segments_per_video,
                )
        except (RuntimeError, subprocess.CalledProcessError, ValueError) as error:
            print(f"SKIP {path.name}: {error}", flush=True)
            path.unlink(missing_ok=True)
            continue
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
                "selection_number": len(assets) + 1,
            }
        )
        selected_sources.append(str(candidate))
        if len(assets) == args.count:
            break

    if len(assets) != args.count:
        raise RuntimeError(f"only {len(assets)} valid videos were available")

    # Remove files left by an interrupted or superseded staging attempt.
    accepted = {item["filename"] for item in assets}
    for path in output.iterdir():
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            if path.name not in accepted:
                path.unlink()

    source = (
        {"archive": str(args.archive), "members": selected_sources}
        if args.archive
        else {"directory": str(args.source_dir), "files": selected_sources}
    )

    canonical = {
        "schema_version": 1,
        "selection": {
            "seed": args.seed,
            "count": args.count,
            "excluded_media_ids": sorted(excluded_media_ids),
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

