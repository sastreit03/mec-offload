#!/usr/bin/env python3
"""Measure the production Linux encoding path against a paced frame schedule."""

import argparse
import csv
import hashlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path

from profile_encoding import videos_in


def percentile(values, p):
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def measure_stream(video, bitrate, args, cv2, encoder_class, canonical_au,
                   clock=time.monotonic_ns, sleep=time.sleep):
    """Keep all frames; absolute releases allow catch-up exactly as the sender does.

    Results stay in memory until the stream ends, excluding CSV I/O from timing.
    The measured encoder call includes BGR copying, videoconvert, x264, and parsing.
    """
    # Setup start captures time before opening the video and constructing the encoder
    setup_start = clock()
    capture = cv2.VideoCapture(str(video))
    encoder = None
    try:
        if not capture.isOpened():
            raise RuntimeError(f"unreadable input video: {video}")
        encoder = encoder_class(args.width, args.height, args.fps, bitrate)
        # Capture time when frame schedule begins
        origin = clock()
        rows = []
        frame_id = 0
        while True:
            # When to begin encoding
            release = origin + round(frame_id * 1e9 / args.fps)
            # Deadline when encoding should be finished
            deadline = origin + round((frame_id + 1) * 1e9 / args.fps)
            # Pause execution if the script is ahead of schedule
            remaining = release - clock()
            while remaining > 0:
                sleep(remaining / 1e9)
                remaining = release - clock()
            # Capture how long reading the frame bytes takes
            read_start = clock()
            ok, bgr = capture.read()
            read_end = clock()
            if not ok:
                break
            # Resize the image
            bgr = cv2.resize(bgr, (args.width, args.height))
            resize_end = clock()
            # Encode the image
            gop_frame = frame_id % args.gop_frames
            nals = encoder.frame(bgr, frame_id, args.fps,
                                 keyframe=gop_frame == 0, bitrate=bitrate)
            encode_end = clock()
            if not nals:
                raise RuntimeError(f"empty encoder output: {video} frame {frame_id}")
            kind = "I" if any(n[0] & 31 == 5 for n in nals) else "P"
            if gop_frame == 0 and kind != "I":
                raise RuntimeError(f"missing requested IDR: {video} frame {frame_id}")
            encoded_bytes = len(canonical_au(nals))
            # capture when frame is ready and record timing
            ready = clock()
            rows.append(dict(
                video=str(video), bitrate_bps=bitrate, frame_id=frame_id,
                gop_id=frame_id // args.gop_frames, gop_frame=gop_frame,
                frame_type=kind, encoded_bytes=encoded_bytes,
                scheduled_release_ns=release - origin, deadline_ns=deadline - origin,
                read_start_ns=read_start - origin, read_end_ns=read_end - origin,
                resize_end_ns=resize_end - origin, encode_end_ns=encode_end - origin,
                ready_ns=ready - origin,
                decode_ns=read_end - read_start, resize_ns=resize_end - read_end,
                encoder_call_ns=encode_end - resize_end,
                output_accounting_ns=ready - encode_end,
                processing_ns=ready - read_start,
                start_lateness_ns=max(0, read_start - release),
                release_to_ready_ns=ready - release,
                deadline_lateness_ns=max(0, ready - deadline),
                deadline_missed=ready > deadline,
            ))
            frame_id += 1
        if not rows:
            raise RuntimeError(f"empty input video: {video}")
        return rows, origin - setup_start
    finally:
        try:
            if encoder is not None:
                encoder.close()
        finally:
            capture.release()


def summarize(rows, fps, setup_ns):
    budget = 1e9 / fps
    duration = len(rows) / fps
    elapsed = rows[-1]["ready_ns"] / 1e9
    misses = sum(r["deadline_missed"] for r in rows)
    result = dict(
        video=rows[0]["video"], bitrate_bps=rows[0]["bitrate_bps"],
        frames=len(rows), target_fps=fps, frame_budget_ms=1000 / fps,
        setup_ms=setup_ns / 1e6, media_duration_s=duration,
        elapsed_to_last_output_s=elapsed, paced_output_fps=len(rows) / elapsed,
        processing_capacity_fps=1e9 / statistics.mean(r["processing_ns"] for r in rows),
        encoder_call_capacity_fps=1e9 / statistics.mean(r["encoder_call_ns"] for r in rows),
        encoder_mean_within_budget=statistics.mean(r["encoder_call_ns"] for r in rows) <= budget,
        processing_mean_within_budget=statistics.mean(r["processing_ns"] for r in rows) <= budget,
        deadline_misses=misses, deadline_miss_pct=100 * misses / len(rows),
        all_frames_ready_by_deadline=misses == 0,
        final_deadline_lateness_ms=rows[-1]["deadline_lateness_ns"] / 1e6,
    )
    for field in ("decode", "resize", "encoder_call", "output_accounting", "processing",
                  "start_lateness", "release_to_ready", "deadline_lateness"):
        values = [r[field + "_ns"] / 1e6 for r in rows]
        result.update({field + "_mean_ms": statistics.mean(values),
                       field + "_p95_ms": percentile(values, .95),
                       field + "_p99_ms": percentile(values, .99),
                       field + "_max_ms": max(values)})
    for kind in ("I", "P"):
        values = [r["encoder_call_ns"] / 1e6 for r in rows if r["frame_type"] == kind]
        result[kind + "_frames"] = len(values)
        result[kind + "_encoder_mean_ms"] = statistics.mean(values) if values else ""
        result[kind + "_encoder_p99_ms"] = percentile(values, .99) if values else ""
    return result


def sha256(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    # Read command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--bitrate", type=int, action="append", dest="bitrates", required=True)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--gop-frames", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-videos", type=int, default=8)
    args = parser.parse_args(argv)

    # Validate command line arguments
    if min(*args.bitrates, args.width, args.height, args.fps, args.gop_frames, args.expected_videos) <= 0:
        parser.error("bitrates, dimensions, FPS, GOP, and expected videos must be positive")
    if args.width % 2 or args.height % 2:
        parser.error("width and height must be even")
    videos = videos_in(args.video_dir)
    if len(videos) != args.expected_videos:
        parser.error(f"expected {args.expected_videos} videos, found {len(videos)}")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output directory must be empty or new")

    # Import the production encoder
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "ue/linux/src"))
    sys.path.insert(0, str(root / "contracts/python"))
    from mec_ue.media import Encoder
    from mec_contracts.rtp import canonical_au
    import cv2

    # Create profile.json to contain the profiling metadata
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = dict(status="running", videos=list(map(str, videos)),
                    bitrates_bps=sorted(set(args.bitrates)), fps=args.fps,
                    gop_frames=args.gop_frames, width=args.width, height=args.height,
                    encoder="multi-ue/ue/linux/src/mec_ue/media.py:Encoder",
                    schedule="absolute FPS releases; retain every frame; catch up when late",
                    deadline="encoded output accounted for by next scheduled frame release",
                    transport_included=False, warmup_frames_excluded=0,
                    timestamp_clock="monotonic_ns; CSV timestamps relative to each stream origin",
                    platform=platform.platform(), python=sys.version, opencv=cv2.__version__,
                    source_sha256={},
                    encoder_source_sha256=sha256(root / "ue/linux/src/mec_ue/media.py"),
                    profiler_source_sha256=sha256(Path(__file__)))
    meta_path = args.output / "profile.json"
    def save_metadata():
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n")
    save_metadata()
    summaries = []
    expected_counts = {}
    try:
        metadata["source_sha256"] = {str(v): sha256(v) for v in videos}
        save_metadata()
        with (args.output / "frame-timings.csv").open("w", newline="") as stream:
            writer = None
            # Profile for each rate and each video
            for rate in metadata["bitrates_bps"]:
                for video in videos:
                    print(f"Profiling {video.name} at {rate} bps, {args.fps} FPS", flush=True)
                    # Open the video and start timing frame encoding
                    rows, setup_ns = measure_stream(video, rate, args, cv2, Encoder, canonical_au)
                    if expected_counts.setdefault(video, len(rows)) != len(rows):
                        raise RuntimeError(f"frame count changed across bitrates: {video}")
                    if writer is None:
                        # Create CSV writer and write header
                        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                        writer.writeheader()
                    writer.writerows(rows)
                    stream.flush()
                    # Summarize frame timing and writ to summary.csv
                    summary = summarize(rows, args.fps, setup_ns)
                    summaries.append(summary)
                    write_csv(args.output / "summary.csv", summaries)
                    print(f"  encoder mean={summary['encoder_call_mean_ms']:.2f} ms; "
                          f"processing p99={summary['processing_p99_ms']:.2f} ms; "
                          f"deadline misses={summary['deadline_misses']}/{len(rows)}; "
                          f"final lateness={summary['final_deadline_lateness_ms']:.2f} ms", flush=True)
        metadata["status"] = "complete"
        metadata["all_streams_meet_frame_deadlines"] = all(
            s["all_frames_ready_by_deadline"] for s in summaries)
    except BaseException as error:
        metadata["status"] = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        metadata["error"] = repr(error)
        raise
    finally:
        save_metadata()
    print(f"Results: {args.output}. Review deadline misses; completion is not a speed PASS.")


if __name__ == "__main__":
    main()
