#!/usr/bin/env python3
"""Measure access-unit sizes using the Linux UE's production H.264 encoder."""

import argparse
import csv
import json
import sys
from pathlib import Path


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi"}


def videos_in(directory):
    return [
        path.resolve()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument(
        "--bitrate",
        type=int,
        action="append",
        dest="bitrates",
        required=True,
        help="Target bitrate in bits/s; repeat for every candidate bitrate",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--gop-frames", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if min(args.bitrates) <= 0 or min(args.width, args.height, args.fps, args.gop_frames) <= 0:
        parser.error("bitrates and video parameters must be positive")
    videos = videos_in(args.video_dir)
    if not videos:
        parser.error("--video-dir contains no supported video files")

    # Import the production encoder directly so profiling cannot drift from UE settings.
    ue_source = Path(__file__).resolve().parents[1] / "ue" / "linux" / "src"
    sys.path.insert(0, str(ue_source))
    from mec_contracts.rtp import canonical_au
    from mec_ue.media import Encoder
    import cv2

    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "videos": [str(path) for path in videos],
        "bitrates_bps": sorted(set(args.bitrates)),
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "gop_frames": args.gop_frames,
        "encoder": "multi-ue/ue/linux/src/mec_ue/media.py:Encoder",
    }
    (args.output / "profile.json").write_text(json.dumps(metadata, indent=2))

    fields = [
        "video",
        "bitrate_bps",
        "frame_id",
        "gop_id",
        "gop_frame",
        "frame_type",
        "encoded_bytes",
    ]
    with (args.output / "frame-sizes.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for bitrate in sorted(set(args.bitrates)):
            for video in videos:
                capture = cv2.VideoCapture(str(video))
                if not capture.isOpened():
                    raise RuntimeError(f"unreadable input video: {video}")
                encoder = Encoder(args.width, args.height, args.fps, bitrate)
                try:
                    frame_id = 0
                    while True:
                        ok, bgr = capture.read()
                        if not ok:
                            break
                        bgr = cv2.resize(bgr, (args.width, args.height))
                        gop_frame = frame_id % args.gop_frames
                        nals = encoder.frame(
                            bgr,
                            frame_id,
                            args.fps,
                            keyframe=gop_frame == 0,
                            bitrate=bitrate,
                        )
                        frame_type = "I" if any(nal[0] & 31 == 5 for nal in nals) else "P"
                        if gop_frame == 0 and frame_type != "I":
                            raise RuntimeError(
                                f"encoder missed forced keyframe: {video} frame {frame_id}"
                            )
                        writer.writerow(
                            {
                                "video": str(video),
                                "bitrate_bps": bitrate,
                                "frame_id": frame_id,
                                "gop_id": frame_id // args.gop_frames,
                                "gop_frame": gop_frame,
                                "frame_type": frame_type,
                                "encoded_bytes": len(canonical_au(nals)),
                            }
                        )
                        frame_id += 1
                    if frame_id == 0:
                        raise RuntimeError(f"empty input video: {video}")
                    output.flush()
                finally:
                    encoder.close()
                    capture.release()


if __name__ == "__main__":
    main()
