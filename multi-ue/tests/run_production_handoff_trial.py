"""Isolated virtual-UE trial: real CUDA models/ByteTrack, injected decoded media.

No RF, RTP, GStreamer, admission, or radio-control claim is made by this harness.
Run from multi-ue with its normal PYTHONPATH (see queue validation documentation).
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "mec/src"), str(ROOT / "contracts/python")]
from mec_contracts.rtp import Descriptor
from mec_server.config import load
from mec_server.engine import Engine, Video
from mec_server.tracking import Tracker


def media_frames(path, count):
    import cv2
    cap = cv2.VideoCapture(str(path))
    frames = []
    try:
        for _ in range(count):
            ok, bgr = cap.read()
            if not ok:
                raise RuntimeError("Media ended before the requested frame count")
            frames.append(cv2.cvtColor(cv2.resize(bgr, (1280, 720)), cv2.COLOR_BGR2RGB))
    finally:
        cap.release()
    return frames


async def trial(args):
    cfg, catalog, profiles = load(ROOT / "config/mec.collection.yaml")
    cfg.applications = ("detection", "tracking", "segmentation")
    cfg.backend = args.backend
    cfg.mps_enabled = False
    cfg.priority_streams = (-3,)
    cfg.warmup_count = 3
    cfg.run_id = args.run_id
    cfg.runs_dir = str(Path(args.output).resolve())
    cfg.weights_dir = str(Path(args.weights).resolve())
    if (Path(cfg.runs_dir) / cfg.run_id).exists():
        raise ValueError("Choose a fresh run ID to preserve existing evidence")
    frames = media_frames(args.media, args.frames)
    engine = Engine(cfg, catalog, profiles)
    engine.loop = asyncio.get_running_loop()
    submitted, completions, payloads, tracking_order = [], [], {}, {}
    original_submit, original_poll = engine.workers.submit, engine.workers.poll

    def submit(app, priority, job):
        accepted = original_submit(app, priority, job)
        if accepted:
            submitted.append(tuple(job["job_id"]))
        return accepted

    def poll():
        for message in original_poll():
            if message["type"] in ("completed", "failed"):
                completions.append((tuple(message["job_id"]), message["type"]))
            yield message

    def receive(message):
        if message["type"] == "frame_result" and hasattr(message, "text"):
            value = json.loads(message.text)
            payloads[(value["video_id"], value["frame_id"])] = value.get("payload")

    engine.workers.submit, engine.workers.poll = submit, poll
    engine.workers.start()
    engine.tasks = [asyncio.create_task(engine.tick())]
    selections = ["detection-yolo26x-500k", "tracking-yolo26x-500k",
                  "tracking-yolo26x-500k", "segmentation-segformer-b3-500k"]
    videos = []
    try:
        async with asyncio.timeout(180):
            while len(engine.workers.ready) != 3:
                if engine.worker_failures:
                    raise RuntimeError(engine.worker_failures)
                if engine.tasks[0].done():
                    engine.tasks[0].result()
                await asyncio.sleep(.02)
        for i, cid in enumerate(selections):
            candidate = engine.candidates[cid]
            user, _ = engine.hello(None, f"12.1.1.{i + 1}", receive)
            user.alias = f"virtual-ue-{i + 1}"
            vid = str(uuid.uuid4())
            video = Video(vid, user.uid, candidate["application"], 1,
                          time.monotonic(), state="ACTIVE")
            plan = dict(candidate_id=cid, config_version=1, plan_version=1,
                        fps=cfg.fps, gop_frames=cfg.gop_frames, epoch_unix_ns=time.time_ns(),
                        offset_frames=0, activation_cycle=0, boundary_unix_ns=time.time_ns())
            video.configs[1], video.plans[1], video.current = candidate, plan, plan
            if video.app == "tracking":
                tracker = Tracker(cfg.fps, cfg.tracker_retention_s, mock=args.backend == "mock")
                tracking_order[vid] = []
                update = tracker.update

                def track(fid, payload=None, update=update, vid=vid):
                    tracking_order[vid].append(fid)
                    return update(fid, payload)

                tracker.update = track
                video.tracker = tracker
            handoff = engine.receiver_handoff(vid)
            video.receiver = SimpleNamespace(post=handoff, tick=lambda: None, close=lambda: None)
            engine.videos[vid] = video
            user.active_video = vid
            videos.append(video)

        def publish(fid):
            for video in videos:
                now, wall = time.monotonic_ns(), time.time_ns()
                descriptor = Descriptor(video.vid, 1, fid, fid // cfg.gop_frames,
                                        1, 1, 1, 100, now, now, wall, wall, 15)
                handoff = video.receiver.post
                handoff("descriptor", dict(descriptor=descriptor, first_packet_ns=now))
                handoff("progress", dict(descriptor=descriptor, unique_bytes=100,
                                         complete=True, received_ns=time.monotonic_ns()))
                handoff("decoded", dict(descriptor=descriptor, rgb=frames[fid],
                                        decoded_ns=time.monotonic_ns()))

        began = time.monotonic()
        for fid in range(args.frames):
            await asyncio.sleep(max(0, began + fid / cfg.fps - time.monotonic()))
            await asyncio.to_thread(publish, fid)
        async with asyncio.timeout(180):
            while any(v.receiver.post.scheduled for v in videos):
                await asyncio.sleep(.001)
            for video in videos:
                video.final_id = args.frames - 1
                video.frames_fully_sent = args.frames
                video.input_close_at = time.monotonic()
            while not all(v.finalized for v in videos) or engine.inflight:
                if engine.tasks[0].done():
                    engine.tasks[0].result()
                await asyncio.sleep(.01)
        before_close = dict(frames=len(engine.frames), inflight=len(engine.inflight),
                            deliveries=sum(v.deliveries for v in videos),
                            tracking_busy=any(v.tracker_busy for v in videos))
    finally:
        await engine.close()

    jobs = [json.loads(line) for line in (engine.root / "jobs.jsonl").read_text().splitlines()]
    counts = Counter((j["video_id"], j["frame_id"]) for j in jobs)
    accepted = Counter(submitted)
    returned = Counter(key for key, _ in completions)
    launch_ms = [(j["stage_times"]["gpu_submit_ns"] - j["stage_times"]["decoded_monotonic_ns"]) / 1e6
                 for j in jobs if "gpu_submit_ns" in j["stage_times"]]
    events = [json.loads(line) for line in (engine.root / "events.jsonl").read_text().splitlines()]
    errors = [e for e in events if e.get("type") in
              ("receiver_event_error", "background_error", "tracker_error", "discarded_gpu_completion")]
    report = dict(kind="isolated decoded-input virtual-UE trial", backend=args.backend,
                  radio_and_decoder_tested=False, media=str(args.media), frames_per_ue=args.frames,
                  fps_per_ue=cfg.fps, models=selections, submitted=len(submitted),
                  terminal_records=len(jobs), outcomes=dict(Counter(j["outcome"] for j in jobs)),
                  exactly_one_terminal_per_submitted=counts == accepted and all(n == 1 for n in counts.values()),
                  exactly_one_worker_result_per_submitted=returned == accepted,
                  receiver_queues={v.vid: v.receiver.post.stats() for v in videos},
                  decoded_to_model_submit_ms=dict(zip(("p95", "p99"), map(float, np.percentile(launch_ms, [95, 99])))) if launch_ms else None,
                  tracking_order=tracking_order,
                  tracking_order_correct=all(ids == list(range(args.frames)) for ids in tracking_order.values()),
                  before_close=before_close,
                  worker_exitcodes={a: p.exitcode for a, p in engine.workers.processes.items()},
                  worker_drain_clean=all(p.exitcode == 0 and not p.is_alive() for p in engine.workers.processes.values()),
                  scenario_error=engine.scenario_error, errors=errors)
    report["model_sha256"] = {cid: hashlib.sha256((Path(cfg.weights_dir) / engine.candidates[cid]["artifact"]).read_bytes()).hexdigest()
                              for cid in set(selections)} if args.backend != "mock" else {}
    if args.reference_source and args.backend != "mock":
        spec = importlib.util.spec_from_file_location("baseline_workers", args.reference_source)
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        comparisons = {}
        for video, cid in zip(videos, selections):
            c = engine.candidates[cid]
            adapter = reference.TorchAdapter({c["model"]: c}, cfg.weights_dir, [-3], False)
            results = []
            for fid in (0, args.frames - 1):
                old, _ = adapter.infer(dict(configuration=c, priority=-3, rgb=frames[fid]))
                new = payloads[(video.vid, fid)]
                # Tracking adds fields after detector formatting; compare detector output.
                if video.app == "tracking":
                    new = {k: new[k] for k in old}
                    new["schema"] = old["schema"]
                results.append(dict(frame_id=fid, exact_payload_match=old == new))
            comparisons[video.vid] = results
            del adapter
        report["baseline_comparisons"] = comparisons
    (engine.root / "trial-summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    assert len(submitted) == len(videos) * args.frames
    assert report["exactly_one_terminal_per_submitted"] and report["exactly_one_worker_result_per_submitted"]
    assert report["tracking_order_correct"] and report["worker_drain_clean"]
    assert not any(before_close.values())
    assert report["outcomes"] == {"completed": len(submitted)}
    assert not engine.scenario_error and not errors
    assert all(not q["overflow_notifications"] for q in report["receiver_queues"].values())
    if "baseline_comparisons" in report:
        assert all(x["exact_payload_match"] for rows in report["baseline_comparisons"].values() for x in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", required=True)
    parser.add_argument("--weights", default="weights")
    parser.add_argument("--output", default="runs")
    parser.add_argument("--run-id", default="queue-trial-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--backend", choices=("torchscript", "mock"), default="torchscript")
    parser.add_argument("--reference-source", help="Pre-update workers.py for identical-input output comparison")
    asyncio.run(trial(parser.parse_args()))
