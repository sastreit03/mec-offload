#!/usr/bin/env python3
"""Sample real target worker times. This is profiling, not pipeline/live acceptance."""

import argparse
import csv
import json
import time
from pathlib import Path
import yaml


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi"}


def input_videos(args, parser):
    videos = [Path(value) for value in args.video]
    if args.video_dir:
        videos.extend(
            path
            for path in sorted(args.video_dir.rglob("*"))
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )
    # Preserve the requested order while removing duplicate paths.
    videos = list(dict.fromkeys(path.resolve() for path in videos))
    missing = [str(path) for path in videos if not path.is_file()]
    if missing:
        parser.error(f"missing input video(s): {', '.join(missing)}")
    if not videos:
        parser.error("provide --video or --video-dir with at least one video")
    return videos


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", action="append", default=[])
    p.add_argument("--video-dir", type=Path)
    p.add_argument("--catalog", default="config/tasks.yaml")
    p.add_argument("--weights", default="weights")
    p.add_argument("--samples", type=int, default=30)
    p.add_argument("--warmup", type=int, required=True)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--tracker-retention-s", type=float, default=1.0)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    from mec_server.workers import Workers, DEFAULT_PRIORITIES
    from mec_server.tracking import Tracker
    import cv2

    videos = input_videos(a, p)
    if a.samples < len(videos):
        p.error("--samples must be at least the number of input videos")
    catalog = yaml.safe_load(Path(a.catalog).read_text())
    workers = Workers(
        catalog,
        dict(backend="torchscript", weights_dir=a.weights, warmup_count=a.warmup),
    )
    workers.start()
    a.output.mkdir(parents=True, exist_ok=True)
    try:
        until = time.monotonic() + 600
        while len(workers.ready) < 3:
            for m in workers.poll():
                if m["type"] == "startup_failed":
                    raise RuntimeError(m)
            if time.monotonic() > until:
                raise TimeoutError("warmup readiness")
            time.sleep(0.01)
        (a.output / "workers.json").write_text(json.dumps(workers.ready, indent=2))
        # Keep the sampling inputs with the measurements for later profile provenance.
        (a.output / "profile.json").write_text(
            json.dumps(
                dict(
                    videos=[str(path) for path in videos],
                    samples_per_model=a.samples,
                    warmup_count=a.warmup,
                    fps=a.fps,
                    tracker_retention_s=a.tracker_retention_s,
                    catalog=str(Path(a.catalog).resolve()),
                    weights=str(Path(a.weights).resolve()),
                ),
                indent=2,
            )
        )
        # Measure each resident model once per application, across the three priorities.
        unique = {(c["application"], c["model"]): c for c in catalog["configurations"]}
        with (a.output / "processing.csv").open("w") as f:
            fields = [
                "video",
                "frame_id",
                "application",
                "model",
                "input_hw",
                "precision",
                "priority",
                "processing_s",
                "preprocess_s",
                "inference_s",
                "postprocess_s",
                "cuda_event_ms",
                "gpu_submit_ns",
                "gpu_completion_observed_ns",
                "tracker_s",
            ]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for c in unique.values():
                cap = None
                active_video = None
                tracker = None
                # Use a contiguous block from every video so tracking state advances normally.
                samples_per_video = (a.samples + len(videos) - 1) // len(videos)
                for n in range(a.samples):
                    video = videos[min(n // samples_per_video, len(videos) - 1)]
                    if video != active_video:
                        if cap is not None:
                            cap.release()
                        cap = cv2.VideoCapture(str(video))
                        if not cap.isOpened():
                            raise RuntimeError(f"unreadable input video: {video}")
                        # Sample a centered contiguous block instead of only each clip's opening frames.
                        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                        block_start = max(0, (frame_count - samples_per_video) // 2)
                        cap.set(cv2.CAP_PROP_POS_FRAMES, block_start)
                        active_video = video
                        tracker = (
                            Tracker(a.fps, a.tracker_retention_s)
                            if c["application"] == "tracking"
                            else None
                        )
                    frame_id = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    ok, bgr = cap.read()
                    if not ok:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        frame_id = 0
                        ok, bgr = cap.read()
                        if tracker is not None:
                            tracker = Tracker(a.fps, a.tracker_retention_s)
                    if not ok:
                        raise RuntimeError(f"empty input video: {video}")
                    rgb = cv2.cvtColor(cv2.resize(bgr, (1280, 720)), cv2.COLOR_BGR2RGB)
                    priority = DEFAULT_PRIORITIES[n % len(DEFAULT_PRIORITIES)]
                    workers.submit(
                        c["application"],
                        priority,
                        dict(
                            job_id=[c["id"], n],
                            priority=priority,
                            configuration=c,
                            rgb=rgb,
                        ),
                    )
                    while True:
                        result = list(workers.poll())
                        if result:
                            break
                        time.sleep(0.001)
                    m = result[-1]
                    if m["type"] != "completed":
                        raise RuntimeError(m)
                    tracker_s = ""
                    if tracker is not None:
                        tracker_start = time.monotonic_ns()
                        tracker.update(frame_id, m["payload"])
                        tracker_s = (time.monotonic_ns() - tracker_start) / 1e9
                    writer.writerow(
                        dict(
                            video=str(video),
                            frame_id=frame_id,
                            application=c["application"],
                            model=c["model"],
                            input_hw=str(c["input_hw"]),
                            precision=c["precision"],
                            priority=priority,
                            tracker_s=tracker_s,
                            **m["times"],
                        )
                    )
                    f.flush()
                if cap is not None:
                    cap.release()
    finally:
        workers.close()


if __name__ == "__main__":
    main()
