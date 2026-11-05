#!/usr/bin/env python3
"""Validate Linux encoder profiling artifacts and summarize each bitrate."""

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def percentile(values, probability):
    """Calculates requested measurement percentiles, e.g., p50, p95, p99"""
    values = sorted(values)
    position = (len(values) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def validate(args):
    # Read profile.json, which has the profiling settings/metadata used
    metadata = json.loads((args.profile_dir / "profile.json").read_text())
    bitrates = sorted(set(args.bitrates))
    for key, expected in {
        "bitrates_bps": bitrates,
        "fps": args.fps,
        "gop_frames": args.gop_frames,
        "width": args.width,
        "height": args.height,
    }.items():
        # Check if metadata from profile.json matches expected command line arguments
        if metadata.get(key) != expected:
            raise ValueError(
                f"profile.json {key}: expected {expected}, found {metadata.get(key)}"
            )
    # Check that there are the expected number of videos
    videos = metadata.get("videos", [])
    if len(videos) != args.expected_videos or len(set(videos)) != len(videos):
        raise ValueError(f"expected {args.expected_videos} distinct videos in profile.json")

    by_video = defaultdict(list)    # For video bitrate/frame count calculations
    by_type = defaultdict(list)     # For frame type statistics
    # Read the CSV with the byte counts by frame
    with (args.profile_dir / "frame-sizes.csv").open(newline="") as source:
        # Create CSV reader
        reader = csv.DictReader(source)
        required = {
            "video", "bitrate_bps", "frame_id", "gop_id", "gop_frame",
            "frame_type", "encoded_bytes",
        }
        # Check header fields
        if not required <= set(reader.fieldnames or []):
            raise ValueError("frame-sizes.csv is missing required columns")
        for number, row in enumerate(reader, 2):
            # Read data for this row
            bitrate = int(row["bitrate_bps"])
            video = row["video"]
            fid = int(row["frame_id"])
            frame_type = row["frame_type"]
            size = int(row["encoded_bytes"])

            # Validate data for this row
            if bitrate not in bitrates or video not in videos:
                raise ValueError(f"row {number}: unexpected bitrate or video")
            if frame_type not in {"I", "P"} or size <= 0:
                raise ValueError(f"row {number}: invalid frame type or size")
            if fid != len(by_video[(bitrate, video)]):
                raise ValueError(f"row {number}: non-contiguous frame IDs for {bitrate}/{video}")
            if (
                int(row["gop_id"]) != fid // args.gop_frames
                or int(row["gop_frame"]) != fid % args.gop_frames
            ):
                raise ValueError(f"row {number}: GOP position does not match --gop-frames")
            if fid % args.gop_frames == 0 and frame_type != "I":
                raise ValueError(f"row {number}: GOP boundary is not I")

            # Add frame data to dictionaries
            by_video[(bitrate, video)].append(size)
            by_type[(bitrate, frame_type)].append(size)

    # Check every video exists at every bitrate
    for video in videos:
        counts = [len(by_video[(bitrate, video)]) for bitrate in bitrates]
        if not all(counts):
            raise ValueError(f"missing frame rows for one or more bitrates: {video}")
        if len(set(counts)) != 1:
            raise ValueError(f"frame counts differ across bitrates for {video}: {counts}")

    summary = []
    # Process results first by bitrate, then by frame type
    for bitrate in bitrates:
        for frame_type in ("I", "P"):
            values = by_type[(bitrate, frame_type)]
            # An explicitly all-intra GOP has no P frames to summarize.
            if not values and frame_type == "P" and args.gop_frames == 1:
                continue
            if not values:
                raise ValueError(f"no {frame_type}-frame samples at {bitrate} bit/s")
            summary.append({
                "bitrate_bps": bitrate,
                "frame_type": frame_type,
                "samples": len(values),
                "mean_bytes": statistics.mean(values),
                "p50_bytes": percentile(values, 0.50),
                "p95_bytes": percentile(values, 0.95),
                "p99_bytes": percentile(values, 0.99),
            })
    return summary, by_video


def main(argv=None):
    # Read command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--bitrate", type=int, action="append", dest="bitrates", required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--gop-frames", type=int, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--expected-videos", type=int, default=8)
    args = parser.parse_args(argv)

    # Validate arguments
    if min(*args.bitrates, args.fps, args.gop_frames, args.width,
           args.height, args.expected_videos) <= 0:
        parser.error("bitrates, video parameters, and expected video count must be positive")
    try:
        summary, by_video = validate(args)
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        parser.exit(1, f"Encoding profile validation: FAIL: {error}\n")

    # Open summary file
    output = args.profile_dir / "summary.csv"
    with output.open("w", newline="") as stream:
        # Create CSV writer and write header
        writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)


    for bitrate in sorted(set(args.bitrates)):
        print(f"requested_bps={bitrate}")
        all_sizes = []
        for (rate, video), sizes in sorted(by_video.items()):
            if rate != bitrate:
                continue
            all_sizes.extend(sizes)
            produced = sum(sizes) * 8 / (len(sizes) / args.fps) # Produced bps
            print(f"  video={video} frames={len(sizes)} produced_bps={produced:.0f} "
                  f"produced/requested={produced / bitrate:.4f}")
        for item in summary:
            if item["bitrate_bps"] == bitrate:
                print(f"  {item['frame_type']} samples={item['samples']} "
                      f"mean_bytes={item['mean_bytes']:.3f} "
                      f"p50_bytes={item['p50_bytes']:.3f} "
                      f"p95_bytes={item['p95_bytes']:.3f} "
                      f"p99_bytes={item['p99_bytes']:.3f}")
        # Calculate produced bps across all frame sizes at this bitrate
        duration = len(all_sizes) / args.fps
        produced = sum(all_sizes) * 8 / duration
        print(f"  aggregate frames={len(all_sizes)} media_duration_s={duration:.3f} "
              f"produced_bps={produced:.0f} produced/requested={produced / bitrate:.4f}")
    print(f"Summary: {output}")
    print("Encoding profile validation: PASS")


if __name__ == "__main__":
    main()
