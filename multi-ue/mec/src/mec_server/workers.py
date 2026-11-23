"""Configured application processes with immutable models and priority streams."""

import asyncio
import concurrent.futures
import ctypes
import ctypes.util
import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from .queue_timing import TimedInputQueue, stamp
from .shared_rgb import RGBLease, RGBMapping

DEFAULT_APPLICATIONS = ("tracking", "detection", "segmentation")
DEFAULT_PRIORITIES = (-5, -3, 0)


def cuda_graph_indices(model, torch):
    """Relocate exported arange indices before the first execution/capture.

    YOLO exports its batch arange on CPU. CUDA advanced indexing then copies
    it implicitly, which is forbidden during capture. Change only the device
    argument of arange; leave shared constants and all other operators intact.
    """
    graph = model.forward.graph
    def visit(block):
        for node in list(block.nodes()):
            for child in node.blocks():
                visit(child)
            if node.kind() != "aten::arange":
                continue
            for index, value in enumerate(node.inputs()):
                if str(value.type()) == "Device" and str(value.toIValue()) == "cpu":
                    with graph.insert_point_guard(node):
                        device = graph.insertConstant(torch.device("cuda", torch.cuda.current_device()))
                    node.replaceInput(index, device)
    visit(graph)
    graph.lint()


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
    input_read_done: object = None
    input_mapping: object = None


class TorchAdapter:
    def __init__(self, model_specs, weights_dir, priorities, mps_enabled=True, cuda_graphs=False, registered_regions=32):
        self.cuda_graphs = cuda_graphs
        self.graphs = {}
        self.rgb_cache = None
        if cuda_graphs:
            from .cuda_rgb import NativeRGB, RegisteredRGB
            self.native_rgb = NativeRGB()
            self.native_rgb.check(self.native_rgb.lib.mapping_flags())
            self.rgb_cache = RegisteredRGB(self.native_rgb, capacity=registered_regions)
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
            if cuda_graphs:
                cuda_graph_indices(model, torch)
            self.models[name] = model

        self.mean = torch.tensor([0.485, 0.456, 0.406], device="cuda", dtype=torch.float16).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device="cuda", dtype=torch.float16).view(1, 3, 1, 1)
        # Model weights and constants were initialized on the default stream.
        torch.cuda.current_stream().synchronize()

    def prepare_graphs(self, specs):
        """Capture before worker readiness. No capture or warmup in live launch."""
        if not self.cuda_graphs:
            return
        t = self.torch
        for model, spec in specs.items():
            for priority, stream in self.streams.items():
                oh, ow = spec["input_hw"]
                with t.inference_mode(), t.cuda.stream(stream):
                    fixed = t.zeros((1,3,oh,ow), dtype=t.float16, device="cuda")
                    for _ in range(5):
                        warm = self.models[model](fixed)
                stream.synchronize()
                graph = t.cuda.CUDAGraph()
                # Each capture owns a private pool; no storage shared between
                # models/priorities that can execute concurrently.
                with t.inference_mode(), t.cuda.graph(graph, stream=stream):
                    output = self.models[model](fixed)
                stream.synchronize()
                self.graphs[model, priority] = (graph, fixed, output)

    def close(self):
        if self.rgb_cache is not None:
            self.torch.cuda.synchronize()
            self.rgb_cache.close()

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
        mapped = job.get("_cuda_input")
        input_read_done = None
        with t.inference_mode(), t.cuda.stream(stream):
            if mapped is not None:
                graph, x, graph_output = self.graphs[c["model"], job["priority"]]
                if tuple(x.shape[2:]) != (oh, ow):
                    raise ValueError("configuration does not match captured model shape")
                host_input = None
                preprocess_start = t.cuda.Event(enable_timing=True)
                preprocess_start.record(stream)
                mark("launch_preprocess_begin")
                self.native_rgb.check(self.native_rgb.lib.preprocess(
                    mapped.pointer, x.data_ptr(), w, h, mapped.descriptor.row_stride,
                    ow, oh, nw, nh, left, top, int(c["application"] == "segmentation"),
                    stream.cuda_stream))
                input_read_done = t.cuda.Event()
                input_read_done.record(stream)
                times.update(cuda_graph=True, shared_rgb_direct=True,
                             graph_input_address=x.data_ptr())
            else:
                mark("launch_pin_begin")
                # Shared input is read-only. Copy once into the existing pinned
                # staging tensor; this stage deliberately retains the RGB upload.
                import numpy as np
                host_input = t.empty(image.shape, dtype=t.uint8, pin_memory=True)
                np.copyto(host_input.numpy(), image)
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
            if mapped is not None:
                graph.replay()
                output = graph_output
            else:
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
                          times, (h, w, scale, top, left), begin, input_read_done, mapped)

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
            TorchAdapter(specs, config["weights_dir"], config["priority_streams"], mps_enabled=mps_enabled,
                         cuda_graphs=config.get("cuda_graphs", True),
                         registered_regions=2*config.get("max_videos", 32))
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
        if adapter and hasattr(adapter, "prepare_graphs"):
            adapter.prepare_graphs(specs)
        if adapter and getattr(adapter, "cuda_graphs", False):
            adapter.torch.cuda.nvtx.range_push("mec_graph_trial")
        outgoing.put(
            dict(
                type="ready",
                application=application,
                pid=os.getpid(),
                models=list(specs),
                streams=list(config["priority_streams"]),
                backend=config["backend"],
                cuda_graphs=bool(adapter and getattr(adapter, "cuda_graphs", False)),
                graph_instances=len(getattr(adapter, "graphs", {})),
            )
        )
    except Exception as ex:
        outgoing.put(
            dict(type="startup_failed", application=application, error=traceback.format_exc())
        )
        return
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(config["priority_streams"])
    )

    def consume_input(job, mapping):
        if mapping is not None:
            job.pop("rgb", None)
            job.pop("_cuda_input", None)
            mapping.close()
            # Result transport must not block the CUDA launch thread.
            message = dict(type="input_released", application=application,
                           job_id=job["job_id"], shared_rgb=job["shared_rgb"])
            if adapter:
                pool.submit(outgoing.put, message)
            else:
                outgoing.put(message)

    def complete(job, submitted, started, mapping=None):
        try:
            finish(job, submitted, started)
        finally:
            consume_input(job, mapping)

    def finish(job, submitted, started):
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
                    if getattr(submission, "input_mapping", None) is not None and submission.input_read_done.query():
                        consume_input(job, submission.input_mapping)
                        submission.input_mapping = None
                        submission.times["shared_rgb_release_observed_ns"] = time.monotonic_ns()
                    if getattr(submission, "input_mapping", None) is not None:
                        waiting.append((job, submission, started))
                        continue
                    if submission.ready.query():
                        submission.times["gpu_completion_observed_ns"] = time.monotonic_ns()
                        pool.submit(complete, job, submission, started)
                    else:
                        waiting.append((job, submission, started))
                except Exception as ex:
                    if getattr(submission, "input_mapping", None) is not None:
                        # A CUDA error cannot justify recycling possibly-active
                        # host memory. Process exit destroys its CUDA context.
                        os._exit(1)
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
        if job.get("command") in ("prepare_rgb", "release_rgb"):
            command_start = time.monotonic_ns()
            try:
                if job["command"] == "prepare_rgb":
                    adapter.rgb_cache.prepare(job["region"])
                    outgoing.put(dict(type="rgb_prepared", application=application,
                                      region=job["region"]["region"],
                                      registration_ms=(time.monotonic_ns()-command_start)/1e6))
                else:
                    adapter.rgb_cache.release(job["region"])
            except Exception as ex:
                outgoing.put(dict(type="rgb_prepare_failed", application=application,
                                  region=job["region"]["region"] if isinstance(job["region"], dict) else job["region"],
                                  error=repr(ex)))
            continue
        started = time.monotonic_ns()
        if get_times:
            stamp(get_times, "worker_get_end")
            job.setdefault("handoff_times", {}).update(get_times)
        mapping = None
        try:
            if "shared_rgb" in job:
                if adapter and getattr(adapter, "cuda_graphs", False):
                    mapping = adapter.rgb_cache.acquire(job["shared_rgb"])
                    job["_cuda_input"] = mapping
                else:
                    mapping = RGBMapping(job["shared_rgb"])
                job["rgb"] = mapping.image
            if not adapter:
                pool.submit(complete, job, None, started, mapping)
                continue
            submission = adapter.launch(job)
        except Exception as ex:
            if job.get("_cuda_input") is not None:
                try:
                    adapter.streams[job["priority"]].synchronize()
                except Exception:
                    os._exit(1)
            consume_input(job, mapping)
            pool.submit(failed, job, started, ex)
            continue
        # launch has synchronously copied the shared view into pinned storage.
        # The submission retains that independent storage through GPU completion.
        if getattr(submission, "input_read_done", None) is None:
            consume_input(job, mapping)
        # Mapped inputs stay leased until the observer sees the RGB-read event.
        job.pop("rgb", None)
        with condition:
            arrivals.append((job, submission, started))
            condition.notify()
    if observer:
        with condition:
            stopping = True
            condition.notify()
        observer.join()
    pool.shutdown(wait=True)
    if adapter and hasattr(adapter, "close"):
        adapter.close()
        if getattr(adapter, "cuda_graphs", False):
            adapter.torch.cuda.nvtx.range_pop()


class Workers:
    def __init__(self, catalog, config):
        ctx = mp.get_context("spawn")
        self.output = ctx.Queue(maxsize=16)
        self.inputs = {}
        self.processes = {}
        self.ready = {}
        self.closing = False
        self.rgb_preparations = {}
        self.direct_rgb = config.get("backend") == "torchscript" and config.get("cuda_graphs", True)
        self.input_leases = {}
        self.lease_lock = threading.RLock()
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
            if m["type"] in ("rgb_prepared", "rgb_prepare_failed"):
                with self.lease_lock:
                    future = self.rgb_preparations.pop((m["application"], m["region"]), None)
                if future is not None and not future.done():
                    if m["type"] == "rgb_prepared":
                        future.set_result(m)
                    else:
                        future.set_exception(RuntimeError(m["error"]))
                yield m
                continue
            if m["type"] == "input_released":
                self.release_input(m["application"], m["job_id"], m["shared_rgb"])
                continue
            if m["type"] == "ready":
                self.ready[m["application"]] = m
            if m["type"] in ("completed", "failed"):
                self.release_input(m["application"], m["job_id"])
                incoming = self.inputs[m["application"]]
                if isinstance(incoming, TimedInputQueue):
                    trace = incoming.take_trace(m["job_id"])
                    m.setdefault("times", {}).update(trace)
                    m["times"]["ipc_sender_trace_available"] = bool(trace)
            yield m
        # Reclaim only after confirmed process exit, never merely a deadline.
        for app, process in self.processes.items():
            if process.exitcode is not None:
                self.release_application(app)

    def release_input(self, app, job_id, wire=None):
        with self.lease_lock:
            key = (app, tuple(job_id))
            lease = self.input_leases.get(key)
            if lease is not None and (wire is None or wire == lease.wire()):
                self.input_leases.pop(key).release()

    async def prepare_rgb(self, app, pool, timeout=30):
        """Wait for worker acknowledgement while the engine continues polling."""
        if not self.direct_rgb:
            return None
        if self.closing or app not in self.ready or not self.processes[app].is_alive():
            raise RuntimeError('application worker unavailable for RGB preparation')
        region = pool.registration()
        key = (app, region['region'])
        if key in self.rgb_preparations:
            raise RuntimeError('RGB preparation already pending')
        future = concurrent.futures.Future()
        with self.lease_lock:
            self.rgb_preparations[key] = future
        try:
            self.inputs[app].put_nowait(dict(command='prepare_rgb', region=region))
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except BaseException:
            # FIFO cleanup also covers timeout/cancellation before worker setup ends.
            self.release_rgb(app, region['region'])
            raise
        finally:
            with self.lease_lock:
                self.rgb_preparations.pop(key, None)

    def release_rgb(self, app, region):
        if not self.closing and self.processes[app].is_alive():
            try:
                self.inputs[app].put_nowait(dict(command='release_rgb', region=region))
            except (queue.Full, OSError, ValueError):
                pass  # Worker shutdown unregisters any retained mappings.

    def release_application(self, app):
        with self.lease_lock:
            for key in list(self.rgb_preparations):
                if key[0] == app:
                    future = self.rgb_preparations.pop(key)
                    try:
                        future.set_exception(RuntimeError('worker exited during RGB preparation'))
                    except concurrent.futures.InvalidStateError:
                        pass  # The awaiting admission may already be cancelled.
            for key in list(self.input_leases):
                if key[0] == app:
                    self.input_leases.pop(key).release()

    def available(self, app, p):
        return (not self.closing and app in self.ready and p in self.ready[app]["streams"]
                and self.processes[app].is_alive())

    # Acceptance into IPC is independent of earlier jobs on the same stream.
    def submit(self, app, p, job):
        if not self.available(app, p):
            return False
        key = (app, tuple(job["job_id"]))
        with self.lease_lock:
            if self.closing or key in self.input_leases:
                return False
            rgb = job.get("rgb")
            if isinstance(rgb, RGBLease):
                job = dict(job)
                job.pop("rgb")
                job["shared_rgb"] = rgb.wire()
                self.input_leases[key] = rgb.retain()
            try:
                if isinstance(self.inputs[app], TimedInputQueue):
                    job = dict(job)
                    job["handoff_times"] = dict(job.get("handoff_times", {}))
                    stamp(job["handoff_times"], "ipc_put_begin")
                self.inputs[app].put_nowait(job)
            except (queue.Full, OSError, ValueError):
                self.release_input(app, job["job_id"])
                return False
        return True

    def close(self):
        with self.lease_lock:
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

        for app in self.processes:
            self.release_application(app)
