#!/usr/bin/env python3
"""Sample real target worker times. This is profiling, not pipeline/live acceptance."""

import argparse
import csv
import json
import time
from pathlib import Path
import yaml


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--catalog", default="config/tasks.yaml")
    p.add_argument("--weights", default="weights")
    p.add_argument("--samples", type=int, default=30)
    p.add_argument("--warmup", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    from mec_server.workers import Workers, DEFAULT_PRIORITIES
    import cv2

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
        cap = cv2.VideoCapture(a.video)
        # Measure each resident model once per application, across the three priorities.
        unique = {(c["application"], c["model"]): c for c in catalog["configurations"]}
        with (a.output / "processing.csv").open("w") as f:
            fields = [
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
            ]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for c in unique.values():
                for n in range(a.samples):
                    ok, bgr = cap.read()
                    if not ok:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, bgr = cap.read()
                    if not ok:
                        raise RuntimeError("empty/unreadable input video")
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
                    writer.writerow(
                        dict(
                            application=c["application"],
                            model=c["model"],
                            input_hw=str(c["input_hw"]),
                            precision=c["precision"],
                            priority=priority,
                            **m["times"],
                        )
                    )
                    f.flush()
        cap.release()
    finally:
        workers.close()


if __name__ == "__main__":
    main()
