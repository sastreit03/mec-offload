"""Configured application processes with immutable models and priority streams."""

import concurrent.futures
import ctypes
import ctypes.util
import multiprocessing as mp
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from .queue_timing import TimedInputQueue, stamp

DEFAULT_APPLICATIONS = ("tracking", "detection", "segmentation")
DEFAULT_PRIORITIES = (-5, -3, 0)


def rle(mask):
    import numpy as np

    a = np.asarray(mask, dtype=np.uint16).reshape(-1)
    if not len(a):
        return []
    boundaries = np.r_[0, np.flatnonzero(a[1:] != a[:-1]) + 1, len(a)]
    return [[int(a[x]), int(y - x)] for x, y in zip(boundaries[:-1], boundaries[1:])]


@dataclass
class Submission:
    # References keep both ends of asynchronous transfers alive until ready.
    host_input: object
    host_output: object
    ready: object
    preprocess_start: object
    model_start: object
    model_end: object
    times: dict
    geometry: tuple
    begin_ns: int


class TorchAdapter:
    def __init__(self, model_specs, weights_dir, priorities, mps_enabled=True):
        import torch

        self.torch = torch
        self.models = {}
        mps_control = (
            Path(os.environ.get("CUDA_MPS_PIPE_DIRECTORY", "/tmp/nvidia-mps"))
            / "control"
        )
        if mps_enabled and not mps_control.exists():
            raise RuntimeError(
                "MPS control pipe not visible; start MPS before workers"
            )
        if not mps_enabled and mps_control.exists():
            raise RuntimeError(
                "MPS is disabled in configuration but its control pipe is present; "
                "stop MEC and MPS before restarting"
            )

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA required")
        
        lib = ctypes.CDLL(ctypes.util.find_library("cudart") or "libcudart.so")
        least, greatest = ctypes.c_int(), ctypes.c_int()
        if (
            lib.cudaDeviceGetStreamPriorityRange(
                ctypes.byref(least), ctypes.byref(greatest)
            )
            != 0
        ):
            raise RuntimeError("priority query failed")
        if any(p < greatest.value or p > least.value for p in priorities):
            raise RuntimeError(
                f"configured priorities outside CUDA range {greatest.value}..{least.value}"
            )
        self.streams = {p: torch.cuda.Stream(priority=p) for p in priorities}
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

        self.mean = torch.tensor([0.485, 0.456, 0.406], device="cuda", dtype=torch.float16).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device="cuda", dtype=torch.float16).view(1, 3, 1, 1)
        # Model weights and constants were initialized on the default stream.
        torch.cuda.current_stream().synchronize()

    # Each job owns its tensors while sharing the application's frozen model weights.
    def launch(self, job):
        import torch.nn.functional as F

        t = self.torch
        stream = self.streams[job["priority"]]
        c = job["configuration"]
        times = {}
        tracing = os.getenv("MEC_TRACE_HANDOFF") == "1"

        def mark(name):
            if tracing:
                stamp(times, name)

        mark("launch_enter")
        begin = time.monotonic_ns()
        image = job["rgb"]
        h, w = image.shape[:2]
        oh, ow = c["input_hw"]
        scale = min(ow / w, oh / h)
        nh, nw = round(h * scale), round(w * scale)
        top, left = (oh - nh) // 2, (ow - nw) // 2
        with t.inference_mode(), t.cuda.stream(stream):
            mark("launch_pin_begin")
            host_input = t.from_numpy(image).pin_memory()
            mark("launch_pin_end")
            preprocess_start = t.cuda.Event(enable_timing=True)
            preprocess_start.record(stream)
            mark("launch_upload_begin")
            uploaded = host_input.to("cuda", non_blocking=True)
            mark("launch_upload_end")
            mark("launch_preprocess_begin")
            x = (
                uploaded
                .permute(2, 0, 1)
                .unsqueeze(0)
                .half()
                / 255
            )
            x = F.interpolate(x, size=(nh, nw), mode="bilinear", align_corners=False)
            x = F.pad(x, (left, ow - nw - left, top, oh - nh - top), value=114 / 255)
            if c["application"] == "segmentation":
                x = (x - self.mean) / self.std
            mark("launch_preprocess_end")
            event0, event1 = (
                t.cuda.Event(enable_timing=True),
                t.cuda.Event(enable_timing=True),
            )
            infer_start = time.monotonic_ns()
            event0.record(stream)
            mark("launch_model_call_begin")
            output = self.models[c["model"]](x)
            mark("launch_model_call_end")
            event1.record(stream)
            mark("launch_output_ops_begin")
            if isinstance(output, dict):
                output = output["logits"]
            if isinstance(output, (tuple, list)):
                output = output[0]
            if c["application"] == "segmentation":
                logits = F.interpolate(
                    output.float(), size=(oh, ow), mode="bilinear", align_corners=False
                )
                logits = logits[:, :, top : top + nh, left : left + nw]
                result = (
                    F.interpolate(
                        logits.argmax(1, keepdim=True).float(),
                        size=(h, w),
                        mode="nearest",
                    )[0, 0]
                    .to(t.int32)
                )
            else:
                result = output[0].float()
            mark("launch_output_ops_end")
            mark("launch_output_alloc_begin")
            host_output = t.empty(result.shape, dtype=result.dtype, device="cpu", pin_memory=True)
            mark("launch_output_alloc_end")
            mark("launch_readback_begin")
            host_output.copy_(result, non_blocking=True)
            mark("launch_readback_end")
            ready = t.cuda.Event(enable_timing=True)
            ready.record(stream)
            times.update(gpu_submit_ns=infer_start, gpu_launch_end_ns=time.monotonic_ns())
        mark("launch_exit")
        return Submission(host_input, host_output, ready, preprocess_start, event0, event1,
                          times, (h, w, scale, top, left), begin)

    def finish(self, job, submission):
        submission.ready.synchronize()
        observed = time.monotonic_ns()
        times = submission.times
        times.setdefault("gpu_completion_observed_ns", observed)
        times["preprocess_s"] = submission.preprocess_start.elapsed_time(submission.model_start) / 1000
        times["cuda_event_ms"] = submission.model_start.elapsed_time(submission.model_end)
        times["inference_s"] = times["cuda_event_ms"] / 1000
        result = submission.host_output.numpy()
        h, w, scale, top, left = submission.geometry
        c = job["configuration"]
        if c["application"] == "segmentation":
            payload = dict(
                schema="segmentation.v1",
                height=h,
                width=w,
                class_map_id=c["class_map_id"],
                encoding="rle_row_major",
                runs=rle(result),
            )
        else:
            boxes = result
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
        times["postprocess_s"] = ((time.monotonic_ns() - observed) / 1e9
                                  + submission.model_end.elapsed_time(submission.ready) / 1000)
        times["processing_s"] = (time.monotonic_ns() - submission.begin_ns) / 1e9
        return payload, times

    def infer(self, job):
        """Synchronous API for warmup and offline profiling."""
        return self.finish(job, self.launch(job))


# CUDA initialization happens in the spawned child, never in the control process.
def worker_main(application, specs, config, incoming, outgoing):
    mps_enabled = config.get("mps_enabled", True)
    if mps_enabled:
        os.environ["CUDA_MPS_CLIENT_PRIORITY"] = "0"
    else:
        os.environ.pop("CUDA_MPS_CLIENT_PRIORITY", None)

    try:
        adapter = (
            TorchAdapter(specs, config["weights_dir"], config["priority_streams"], mps_enabled=mps_enabled)
            if config["backend"] == "torchscript"
            else None
        )
        if adapter:
            import numpy as np

            for model, spec in specs.items():
                for p in config["priority_streams"]:
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
                streams=list(config["priority_streams"]),
                backend=config["backend"],
            )
        )
    except Exception as ex:
        outgoing.put(
            dict(type="startup_failed", application=application, error=repr(ex))
        )
        return
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(config["priority_streams"])
    )

    def complete(job, submitted, started):
        try:
            if adapter:
                payload, times = adapter.finish(job, submitted)
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
            times.update(job.get("handoff_times", {}))
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
            finished = time.monotonic_ns()
            outgoing.put(
                dict(
                    type="failed",
                    job_id=job["job_id"],
                    priority=job["priority"],
                    application=application,
                    error=repr(ex),
                    worker_start_ns=started,
                    worker_finish_ns=finished,
                    times=job.get("handoff_times", {}),
                )
            )

    # This list holds completion records for work already launched on CUDA.
    # One observer checks ready events across streams; CPU formatting and IPC
    # can block a pool thread without holding up the launch thread.
    condition = threading.Condition()
    arrivals = []
    stopping = False

    def failed(job, started, ex):
        outgoing.put(dict(type="failed", job_id=job["job_id"], priority=job["priority"],
                          application=application, error=repr(ex), worker_start_ns=started,
                          worker_finish_ns=time.monotonic_ns(),
                          times=job.get("handoff_times", {})))

    def observe():
        pending = []
        while True:
            with condition:
                if not arrivals:
                    if stopping and not pending:
                        return
                    condition.wait(timeout=0.0005 if pending else None)
                pending.extend(arrivals)
                arrivals.clear()
            waiting = []
            for job, submission, started in pending:
                try:
                    if submission.ready.query():
                        submission.times["gpu_completion_observed_ns"] = time.monotonic_ns()
                        pool.submit(complete, job, submission, started)
                    else:
                        waiting.append((job, submission, started))
                except Exception as ex:
                    pool.submit(failed, job, started, ex)
            pending = waiting

    observer = threading.Thread(target=observe) if adapter else None
    if observer:
        observer.start()
    while True:
        get_times = {}
        if os.getenv("MEC_TRACE_HANDOFF") == "1":
            stamp(get_times, "worker_get_begin")
        job = incoming.get()
        if job is None:
            break
        started = time.monotonic_ns()
        if get_times:
            stamp(get_times, "worker_get_end")
            job.setdefault("handoff_times", {}).update(get_times)
        if not adapter:
            pool.submit(complete, job, None, started)
            continue
        try:
            submission = adapter.launch(job)
        except Exception as ex:
            pool.submit(failed, job, started, ex)
            continue
        # The pinned input is retained by submission until its event fires.
        del job["rgb"]
        with condition:
            arrivals.append((job, submission, started))
            condition.notify()
    if observer:
        with condition:
            stopping = True
            condition.notify()
        observer.join()
    pool.shutdown(wait=True)


class Workers:
    def __init__(self, catalog, config):
        ctx = mp.get_context("spawn")
        self.output = ctx.Queue(maxsize=16)
        self.inputs = {}
        self.processes = {}
        self.ready = {}
        self.closing = False
        applications = tuple(config.get("applications", DEFAULT_APPLICATIONS))
        priorities = tuple(config.get("priority_streams", DEFAULT_PRIORITIES))
        config = dict(config, applications=applications, priority_streams=priorities)
        for app in applications:
            specs = {
                x["model"]: x
                for x in catalog["configurations"]
                if x["application"] == app
            }
            if len(specs) != 4:
                raise ValueError(f"{app} requires four resident model variants")
            q = TimedInputQueue(ctx=ctx) if os.getenv("MEC_TRACE_HANDOFF") == "1" else ctx.Queue()
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
                incoming = self.inputs[m["application"]]
                if isinstance(incoming, TimedInputQueue):
                    trace = incoming.take_trace(m["job_id"])
                    m.setdefault("times", {}).update(trace)
                    m["times"]["ipc_sender_trace_available"] = bool(trace)
            yield m

    def available(self, app, p):
        return (not self.closing and app in self.ready and p in self.ready[app]["streams"]
                and self.processes[app].is_alive())

    # Acceptance into IPC is independent of earlier jobs on the same stream.
    def submit(self, app, p, job):
        if not self.available(app, p):
            return False
        try:
            if isinstance(self.inputs[app], TimedInputQueue):
                job = dict(job)
                job["handoff_times"] = dict(job.get("handoff_times", {}))
                stamp(job["handoff_times"], "ipc_put_begin")
            self.inputs[app].put_nowait(job)
        except (queue.Full, OSError, ValueError):
            return False
        return True

    def close(self):
        self.closing = True
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
