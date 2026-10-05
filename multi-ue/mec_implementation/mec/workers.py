"""Bounded reusable processes. One assigned video task and one in-flight job each."""
import ctypes
import importlib
import json
import multiprocessing as mp
import os
import queue
import subprocess
import time
from dataclasses import dataclass
from uuid import uuid4

from .config import AppConfig
from .records import stamp


def symbol(path):
    module, name = path.split(":", 1)
    return getattr(importlib.import_module(module), name)


def mps_clients():
    def control(command):
        result = subprocess.run(["nvidia-cuda-mps-control"], input=command + "\n",
                                text=True, capture_output=True, timeout=2, check=True)
        return [int(word) for word in result.stdout.split() if word.isdigit()]
    clients = []
    for server in control("get_server_list"):
        clients.extend(control(f"get_client_list {server}"))
    return clients


def worker_main(app_data, commands, replies, require_mps):
    # Set before importing torch or an adapter that might initialize CUDA.
    os.environ["CUDA_MPS_CLIENT_PRIORITY"] = "0"
    app = AppConfig.model_validate(app_data)
    adapter = None
    try:
        torch = None
        streams = {}
        priority_range = None
        if app.device.startswith("cuda"):
            import torch
            torch.cuda.set_device(app.device)
            torch.cuda.init()
            torch.cuda.current_stream()  # Make the context current for the driver query.
            least, greatest = ctypes.c_int(), ctypes.c_int()
            driver = ctypes.CDLL("libcuda.so.1")
            code = driver.cuCtxGetStreamPriorityRange(ctypes.byref(least), ctypes.byref(greatest))
            if code != 0:
                raise RuntimeError(f"CUDA priority range query failed: {code}")
            priority_range = {"low": least.value, "high": greatest.value}
            streams = {p: torch.cuda.Stream(device=app.device, priority=n) for p, n in priority_range.items()}
        adapter = symbol(app.adapter)(app)
        adapter.prepare()
        for _ in range(app.warmup_iterations):
            adapter.warmup()
        if torch:
            torch.cuda.synchronize()
            if require_mps and os.getpid() not in mps_clients():
                raise RuntimeError("Worker PID not attached to MPS; check daemon, user, pipe and PID namespace")
        replies.put({"type": "ready", "pid": os.getpid(), "priority_range": priority_range,
                     "gpu_memory_allocated_bytes": torch.cuda.memory_allocated() if torch else 0,
                     "mps_client_priority": 0, "time": stamp()})
        while True:
            command = commands.get()
            if command["type"] == "stop":
                break
            if command["type"] == "reset":
                adapter.reset()
                replies.put({"type": "reset_done"})
                continue
            start = stamp()
            times = {"processing_start": start}
            duration = None
            try:
                if torch:
                    stream = streams[command["priority"]]
                    # Warm initialization was synchronized. Subsequent jobs stay
                    # on this task's fixed stream; auxiliary streams are adapters' responsibility.
                    with torch.cuda.stream(stream), torch.inference_mode():
                        begin, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                        times["gpu_submit"] = stamp()
                        begin.record(stream)
                        payload = adapter.infer(command["rgb"], command["parameters"])
                        end.record(stream)
                    end.synchronize()  # Only this worker waits, not the control process.
                    times["gpu_complete"] = stamp()
                    duration = begin.elapsed_time(end)
                else:
                    payload = adapter.infer(command["rgb"], command["parameters"])
                times["processing_finish"] = stamp()
                json.dumps(payload, allow_nan=False)  # Fail a bad adapter output here.
                replies.put({"type": "job_done", "payload": payload, "times": times,
                             "cuda_event_ms": duration, "priority_integer": priority_range[command["priority"]] if torch else None})
            except Exception as exc:
                # Ensure launched CUDA work finishes before reset/reuse even if
                # Python postprocessing or serialization raises.
                if torch:
                    torch.cuda.synchronize()
                replies.put({"type": "job_failed", "detail": repr(exc),
                             "times": {**times, "processing_finish": stamp()}})
    except Exception as exc:
        replies.put({"type": "worker_failed", "detail": repr(exc), "time": stamp()})
    finally:
        if adapter:
            try:
                adapter.close()
            except Exception:
                pass


@dataclass
class Worker:
    worker_id: str
    app: AppConfig
    process: object
    commands: object
    replies: object
    state: str = "preparing"
    task_id: str | None = None
    in_flight: bool = False
    created_mono_ns: int = 0
    idle_mono_ns: int = 0
    retire_mono_ns: int = 0


class WorkerPool:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.context = mp.get_context("spawn")  # Never fork a CUDA context.
        self.workers: dict[str, Worker] = {}

    def idle(self, config_id):
        return next((w for w in self.workers.values() if w.app.config_id == config_id and w.state == "idle"), None)

    def claim(self, worker, task_id):
        worker.task_id, worker.state = task_id, "assigned"
        return worker

    def can_prepare(self, app):
        preparing = sum(w.state == "preparing" for w in self.workers.values())
        if preparing >= self.cfg.max_concurrent_preparations:
            return False
        if self.cfg.memory_budget_gib is not None:
            needed = sum(w.app.estimated_gpu_gib or 0 for w in self.workers.values())
            if needed + (app.estimated_gpu_gib or 0) > self.cfg.memory_budget_gib:
                return False
        return len(self.workers) < self.cfg.max_workers

    def prepare(self, app, task_id=None):
        if not self.can_prepare(app):
            return None
        wid = str(uuid4())
        commands, replies = self.context.Queue(maxsize=1), self.context.Queue(maxsize=2)
        process = self.context.Process(target=worker_main, args=(app.model_dump(), commands, replies, self.cfg.require_mps), daemon=True)
        worker = Worker(wid, app, process, commands, replies, task_id=task_id, created_mono_ns=time.monotonic_ns())
        process.start()
        self.workers[wid] = worker
        self.log.event("worker_started", worker_id=wid, task_id=task_id, config_id=app.config_id, pid=process.pid)
        return worker

    def reset(self, worker):
        worker.task_id = None
        worker.in_flight = False
        worker.state = "resetting"
        worker.commands.put_nowait({"type": "reset"})

    def submit(self, worker, frame, parameters, priority):
        assert not worker.in_flight and worker.state == "assigned"
        worker.in_flight = True
        worker.commands.put_nowait({"type": "job", "rgb": frame.rgb,
                                   "parameters": parameters, "priority": priority})

    def poll(self):
        for worker in list(self.workers.values()):
            if worker.state == "retiring":
                if not worker.process.is_alive():
                    self.remove(worker)  # Already exited: no blocking join.
                elif time.monotonic_ns() - worker.retire_mono_ns > 2e9:
                    worker.process.kill()
                continue
            while True:
                if worker.worker_id not in self.workers or worker.state == "retiring":
                    break  # A yielded failure may have removed/closed its queues.
                try:
                    message = worker.replies.get_nowait()
                except queue.Empty:
                    break
                yield worker, message
            if not worker.process.is_alive() and worker.worker_id in self.workers and worker.state != "retiring":
                yield worker, {"type": "worker_failed", "detail": f"Worker exited: {worker.process.exitcode}"}

    def retire(self, worker):
        """Begin cleanup without blocking the control loop or freeing capacity early."""
        assert not worker.in_flight, "Do not preempt submitted inference"
        worker.state, worker.task_id = "retiring", None
        worker.retire_mono_ns = time.monotonic_ns()
        if worker.process.is_alive():
            worker.process.terminate()

    def remove(self, worker):
        self.workers.pop(worker.worker_id, None)
        # Only idle/preparing/crashed workers are evicted during normal operation.
        # Never call remove on an inference-running worker to change scheduling.
        if worker.process.is_alive():
            worker.process.terminate()
        worker.process.join(timeout=1)
        if worker.process.is_alive():
            worker.process.kill()
            worker.process.join(timeout=1)
        for channel in (worker.commands, worker.replies):
            channel.cancel_join_thread()
            channel.close()

    def evict_idle(self, now):
        for worker in list(self.workers.values()):
            if worker.state == "idle" and now - worker.idle_mono_ns > self.cfg.idle_retention_s * 1e9:
                self.log.event("worker_idle_evicted", worker_id=worker.worker_id)
                self.retire(worker)

    def make_space(self, app):
        # FIFO waiting tasks may need a different model. Replace only idle replicas.
        if sum(w.state == "preparing" for w in self.workers.values()) >= self.cfg.max_concurrent_preparations:
            return
        idle = sorted((w for w in self.workers.values() if w.state == "idle" and w.app.config_id != app.config_id),
                      key=lambda w: w.idle_mono_ns)
        if self.cfg.memory_budget_gib is not None:
            active_memory = sum(w.app.estimated_gpu_gib or 0 for w in self.workers.values() if w not in idle)
            if active_memory + (app.estimated_gpu_gib or 0) > self.cfg.memory_budget_gib:
                return
        for worker in idle:
            retained = [w for w in self.workers.values() if w.state != "retiring"]
            projected_memory = sum(w.app.estimated_gpu_gib or 0 for w in retained) + (app.estimated_gpu_gib or 0)
            if len(retained) < self.cfg.max_workers and (self.cfg.memory_budget_gib is None or projected_memory <= self.cfg.memory_budget_gib):
                break
            self.log.event("worker_replaced", worker_id=worker.worker_id, requested_config=app.config_id)
            self.retire(worker)
