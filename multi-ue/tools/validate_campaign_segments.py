#!/usr/bin/env python3
"""Validate the exact random-access operations used by campaign UEs."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
from pathlib import Path


def validate_file(task):
    path, frames, segments = task
    import cv2

    results = []
    for segment in range(segments):
        start = segment * frames
        cap = cv2.VideoCapture(path)
        opened = cap.isOpened()
        metadata_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if opened else 0
        accepted = cap.set(cv2.CAP_PROP_POS_FRAMES, start) if opened else False
        actual = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) if opened else -1
        decoded = 0
        if opened and accepted and actual == start:
            for _ in range(frames):
                ok, _frame = cap.read()
                if not ok:
                    break
                decoded += 1
        cap.release()
        error = None
        if not opened:
            error = "open_failed"
        elif not accepted:
            error = "seek_rejected"
        elif actual != start:
            error = f"seek_mismatch:{actual}"
        elif decoded != frames:
            error = f"decode_incomplete:{decoded}"
        results.append(
            dict(
                file=Path(path).name,
                source_start_frame=start,
                frame_count=frames,
                metadata_frames=metadata_frames,
                seek_accepted=accepted,
                actual_frame=actual,
                decoded_frames=decoded,
                status="pass" if error is None else "fail",
                error=error or "",
            )
        )
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--frames", type=int, default=150)
    parser.add_argument("--segments", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.frames <= 0 or args.segments <= 0 or args.workers <= 0:
        parser.error("frames, segments, and workers must be positive")
    files = sorted(
        path
        for path in args.root.iterdir()
        if path.is_file() and path.suffix.lower() in {".mov", ".mp4", ".mkv"}
    )
    if not files:
        raise SystemExit(f"no videos found in {args.root}")

    rows = []
    tasks = [(str(path), args.frames, args.segments) for path in files]
    with concurrent.futures.ProcessPoolExecutor(args.workers) as pool:
        for result in pool.map(validate_file, tasks):
            rows.extend(result)

    args.output.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with (args.output / "segments.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    failures = [row for row in rows if row["status"] == "fail"]
    summary = {
        "videos": len(files),
        "segments": len(rows),
        "passed": len(rows) - len(failures),
        "failed": len(failures),
        "frames_per_segment": args.frames,
        "segments_per_video": args.segments,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    for row in failures:
        print(
            f"FAIL {row['file']} start={row['source_start_frame']} "
            f"error={row['error']}"
        )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
