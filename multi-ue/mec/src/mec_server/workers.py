"""Three persistent application processes, four immutable model copies each, nine streams."""

import concurrent.futures
import ctypes
import ctypes.util
import multiprocessing as mp
import os
import queue
import threading
import time
from pathlib import Path

PRIORITIES = (-5, -3, 0)


def rle(mask):
    import numpy as np

    a = np.asarray(mask, dtype=np.uint16).reshape(-1)
    if not len(a):
        return []
    boundaries = np.r_[0, np.flatnonzero(a[1:] != a[:-1]) + 1, len(a)]
    return [[int(a[x]), int(y - x)] for x, y in zip(boundaries[:-1], boundaries[1:])]


class TorchAdapter:
    def __init__(self, model_specs, weights_dir):
        import torch

        self.torch = torch
        self.models = {}
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA required")
        if not (
            Path(os.environ.get("CUDA_MPS_PIPE_DIRECTORY", "/tmp/nvidia-mps"))
            / "control"
        ).exists():
            raise RuntimeError("MPS control pipe not visible; start MPS before workers")
        lib = ctypes.CDLL(ctypes.util.find_library("cudart") or "libcudart.so")
        least, greatest = ctypes.c_int(), ctypes.c_int()
        if (
            lib.cudaDeviceGetStreamPriorityRange(
                ctypes.byref(least), ctypes.byref(greatest)
            )
            != 0
        ):
            raise RuntimeError("priority query failed")
        if not greatest.value <= -5 <= -3 <= 0 <= least.value:
            raise RuntimeError(
                f"incompatible CUDA priorities {greatest.value}..{least.value}"
            )
        self.streams = {p: torch.cuda.Stream(priority=p) for p in PRIORITIES}
        for name, spec in model_specs.items():
            path = Path(weights_dir) / spec["artifact"]
            model = torch.jit.freeze(
                torch.jit.load(str(path), map_location="cuda").eval()
            )
            if "prim::SetAttr" in str(model.inlined_graph):
                raise RuntimeError(
                    "mutable TorchScript model; concurrency-safe export required"
                )
            self.models[name] = model

    def infer(self, job):
        import numpy as np
        import torch.nn.functional as F

        t = self.torch
        stream = self.streams[job["priority"]]
        c = job["configuration"]
        times = {}
        begin = time.monotonic_ns()
        image = job["rgb"]
        h, w = image.shape[:2]
        oh, ow = c["input_hw"]
        scale = min(ow / w, oh / h)
        nh, nw = round(h * scale), round(w * scale)
        top, left = (oh - nh) // 2, (ow - nw) // 2
        with t.inference_mode(), t.cuda.stream(stream):
            x = (
                t.as_tensor(image.copy(), device="cuda")
                .permute(2, 0, 1)
                .unsqueeze(0)
                .half()
                / 255
            )
            x = F.interpolate(x, size=(nh, nw), mode="bilinear", align_corners=False)
            x = F.pad(x, (left, ow - nw - left, top, oh - nh - top), value=114 / 255)
            if c["application"] == "segmentation":
                mean = t.tensor(
                    [0.485, 0.456, 0.406], device="cuda", dtype=x.dtype
                ).view(1, 3, 1, 1)
                std = t.tensor(
                    [0.229, 0.224, 0.225], device="cuda", dtype=x.dtype
                ).view(1, 3, 1, 1)
                x = (x - mean) / std
            input_ready = t.cuda.Event()
            input_ready.record(stream)
            input_ready.synchronize()
            times["preprocess_s"] = (time.monotonic_ns() - begin) / 1e9
            event0, event1 = (
                t.cuda.Event(enable_timing=True),
                t.cuda.Event(enable_timing=True),
            )
            infer_start = time.monotonic_ns()
            event0.record(stream)
            output = self.models[c["model"]](x)
            event1.record(stream)
            if isinstance(output, dict):
                output = output["logits"]
            if isinstance(output, (tuple, list)):
                output = output[0]
            # A stream-local event, never device-wide synchronization on the normal path.
            event1.synchronize()
            times["gpu_submit_ns"] = infer_start
            times["gpu_completion_observed_ns"] = time.monotonic_ns()
            times["inference_s"] = (time.monotonic_ns() - infer_start) / 1e9
            times["cuda_event_ms"] = event0.elapsed_time(event1)
            post = time.monotonic_ns()
            if c["application"] == "segmentation":
                logits = F.interpolate(
                    output.float(), size=(oh, ow), mode="bilinear", align_corners=False
                )
                logits = logits[:, :, top : top + nh, left : left + nw]
                labels = (
                    F.interpolate(
                        logits.argmax(1, keepdim=True).float(),
                        size=(h, w),
                        mode="nearest",
                    )[0, 0]
                    .to(t.int32)
                    .cpu()
                    .numpy()
                )
                payload = dict(
                    schema="segmentation.v1",
                    height=h,
                    width=w,
                    class_map_id=c["class_map_id"],
                    encoding="rle_row_major",
                    runs=rle(labels),
                )
            else:
                boxes = output[0].float().cpu().numpy()
                if boxes.ndim != 2 or boxes.shape[1] != 6:
                    raise RuntimeError(
                        "YOLO artifact must export [N,300,6] xyxy/score/class; run export_models.py"
                    )
                detections = []
                for row in boxes:
                    if row[4] < c.get("confidence", 0.1):
                        continue
                    x1, y1, x2, y2, score, cls = row
                    detections.append(
                        dict(
                            box=[
                                max(0.0, min(float(w), (float(x1) - left) / scale)),
                                max(0.0, min(float(h), (float(y1) - top) / scale)),
                                max(0.0, min(float(w), (float(x2) - left) / scale)),
                                max(0.0, min(float(h), (float(y2) - top) / scale)),
                            ],
                            score=float(score),
                            class_id=int(cls),
                        )
                    )
                payload = dict(
                    schema="detection.v1", height=h, width=w, detections=detections
                )
            times["postprocess_s"] = (time.monotonic_ns() - post) / 1e9
        times["processing_s"] = (time.monotonic_ns() - begin) / 1e9
        return payload, times


def worker_main(application, specs, config, incoming, outgoing):
    os.environ["CUDA_MPS_CLIENT_PRIORITY"] = "0"
    try:
        adapter = (
            TorchAdapter(specs, config["weights_dir"])
            if config["backend"] == "torchscript"
            else None
        )
        if adapter:
            import numpy as np

            for model, spec in specs.items():
                for p in PRIORITIES:
                    for _ in range(config["warmup_count"]):
                        adapter.infer(
                            dict(
                                priority=p,
                                configuration=dict(
                                    spec, model=model, application=application
                                ),
                                rgb=np.zeros((720, 1280, 3), dtype=np.uint8),
                            )
                        )
        outgoing.put(
            dict(
                type="ready",
                application=application,
                pid=os.getpid(),
                models=list(specs),
                streams=list(PRIORITIES),
                backend=config["backend"],
            )
        )
    except Exception as ex:
        outgoing.put(
            dict(type="startup_failed", application=application, error=repr(ex))
        )
        return
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)

    def execute(job):
        try:
            started = time.monotonic_ns()
            if adapter:
                payload, times = adapter.infer(job)
            else:
                h, w = job["rgb"].shape[:2]
                time.sleep(config.get("mock_processing_s", 0.001))
                payload = (
                    dict(
                        schema="segmentation.v1",
                        height=h,
                        width=w,
                        class_map_id="synthetic",
                        encoding="rle_row_major",
                        runs=[[0, h * w]],
                    )
                    if application == "segmentation"
                    else dict(schema="detection.v1", height=h, width=w, detections=[])
                )
                times = dict(
                    processing_s=(time.monotonic_ns() - started) / 1e9,
                    preprocess_s=0.0,
                    inference_s=0.0,
                    postprocess_s=0.0,
                )
            outgoing.put(
                dict(
                    type="completed",
                    job_id=job["job_id"],
                    priority=job["priority"],
                    application=application,
                    payload=payload,
                    times=times,
                    worker_start_ns=started,
                    worker_finish_ns=time.monotonic_ns(),
                )
            )
        except Exception as ex:
            outgoing.put(
                dict(
                    type="failed",
                    job_id=job["job_id"],
                    priority=job["priority"],
                    application=application,
                    error=repr(ex),
                )
            )

    while True:
        job = incoming.get()
        if job is None:
            break
        pool.submit(execute, job)
    pool.shutdown(wait=True)


class Workers:
    def __init__(self, catalog, config):
        ctx = mp.get_context("spawn")
        self.output = ctx.Queue(maxsize=16)
        self.inputs = {}
        self.processes = {}
        self.ready = {}
        self.busy = set()
        for app in ("tracking", "detection", "segmentation"):
            specs = {
                x["model"]: x
                for x in catalog["configurations"]
                if x["application"] == app
            }
            if len(specs) != 4:
                raise ValueError(f"{app} requires four resident model variants")
            q = ctx.Queue(maxsize=3)
            self.inputs[app] = q
            self.processes[app] = ctx.Process(
                target=worker_main,
                args=(app, specs, config, q, self.output),
                name=f"mec-{app}",
            )

    def start(self):
        for p in self.processes.values():
            p.start()

    def poll(self):
        while True:
            try:
                m = self.output.get_nowait()
            except queue.Empty:
                break
            if m["type"] == "ready":
                self.ready[m["application"]] = m
            if m["type"] in ("completed", "failed"):
                self.busy.discard((m["application"], m["priority"]))
            yield m

    def available(self, app, p):
        return app in self.ready and (app, p) not in self.busy

    def submit(self, app, p, job):
        if not self.available(app, p):
            return False
        try:
            self.inputs[app].put_nowait(job)
        except queue.Full:
            return False
        self.busy.add((app, p))
        return True

    def close(self):
        for q in self.inputs.values():
            try:
                q.put(None, timeout=1)
            except queue.Full:
                pass
        for p in self.processes.values():
            p.join(timeout=10)
        for p in self.processes.values():
            if p.is_alive():
                p.terminate()
                p.join()
