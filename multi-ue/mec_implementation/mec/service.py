"""Task lifecycle and dispatch. All registry changes run on the asyncio loop.

Only FrameQueue and receiver internals are touched by media threads. No model
or GPU operation runs here; one process per assigned stream owns those objects.
"""
import asyncio
import copy
import importlib.metadata
import platform
import time
from collections import deque
from dataclasses import asdict, dataclass
from uuid import uuid4

from .decision import DecisionProvider, Telemetry
from .frames import FrameQueue
from .media import Receiver
from .records import OffloadRequest, Task, stamp
from .runlog import RunLog
from .workers import WorkerPool, symbol


@dataclass
class Runtime:
    row: Task
    app: object
    frames: FrameQueue
    receiver: Receiver | None = None
    worker: object = None
    in_flight: object = None
    end_received_ns: int | None = None
    input_closed_ns: int | None = None
    stop_task: object = None
    reserved_bytes: int = 0


class MEC:
    def __init__(self, cfg, catalog):
        self.cfg, self.catalog = cfg, catalog
        self.log = RunLog(cfg.logs_dir)
        self.started = stamp()
        self.versions = {"python": platform.python_version()}
        for package in ("pydantic", "websockets", "numpy", "Pillow", "torch", "ultralytics"):
            try:
                self.versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                pass
        self.pool = WorkerPool(cfg.pool, self.log)
        self.telemetry = Telemetry(cfg.ric)
        self.decisions = DecisionProvider(cfg.ric, catalog, self.telemetry, self.log)
        self.ues, self.tasks, self.runtimes = {}, {}, {}
        self.dedup = {}
        self.waiting = deque()
        self.recent_jobs = deque(maxlen=cfg.recent_jobs)
        self.admission_lock = asyncio.Lock()
        self.hub = None
        self.loop = None
        self.running = False
        self.background = set()
        self.snapshot_ns = 0
        self.initial_replicas_pending = deque(cid for cid, n in cfg.pool.initial_replicas.items() for _ in range(n))

    def background_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def summary(self):
        return {"waiting_tasks": len(self.waiting), "max_waiting_tasks": self.cfg.queues.max_waiting_tasks,
                "workers": [{"worker_id": w.worker_id, "config_id": w.app.config_id,
                             "state": w.state, "task_id": w.task_id} for w in self.pool.workers.values()]}

    async def offload(self, ue, raw):
        request = OffloadRequest.model_validate(raw)
        if request.ue_id != ue.ue_id:
            raise ValueError("Request ue_id does not match this connection")
        async with self.admission_lock:
            key = (ue.ue_id, request.client_seq)
            canonical = request.model_dump()
            # IP and retry-send timestamp are connection observations, not job identity.
            fingerprint = {k: v for k, v in canonical.items() if k not in {"ip_address", "ue_request_send_unix_ns"}}
            ue.reported_ip, ue.last_seen = request.ip_address, stamp()
            self.background_task(self.decisions.associate(ue, self.cfg.timeouts.decision_s))
            if key in self.dedup:
                previous, task_id = self.dedup[key]
                if previous != fingerprint:
                    raise ValueError("client_seq already used for a different request")
                row = self.tasks[task_id]
                response = copy.deepcopy(row.response)
                response["current_status"] = row.status
                return response
            task_id = str(uuid4())
            row = Task(task_id, ue.ue_id, request.client_seq, canonical)
            row.times["request_receive"] = stamp()
            self.tasks[task_id] = row
            self.dedup[key] = (fingerprint, task_id)
            ue.task_ids.append(task_id)
            self.log.event("task_requested", task_id=task_id, ue_id=ue.ue_id, client_seq=request.client_seq)
            row.times["decision_start"] = stamp()
            plan, source = await self.decisions.decide(task_id, request, self.summary(), self.cfg.timeouts.decision_s)
            row.times["decision_end"] = stamp()
            row.decision_source = source
            response = {"type": "offload_response", "ue_id": ue.ue_id, "task_id": task_id,
                        "client_seq": request.client_seq, "accepted": False, "status": "denied"}
            if not plan.accepted:
                return self._deny(row, response, "policy_denied" if source == "ric" else plan.reason)
            app = self.catalog.get(plan.config_id)
            # Existing compatible waiters get the idle worker before a newcomer.
            self._assign_idle_workers()
            worker = self.pool.idle(app.config_id)
            if worker is None and len(self.waiting) >= self.cfg.queues.max_waiting_tasks:
                return self._deny(row, response, "capacity_limit")
            reserved = app.width * app.height * 3 * (self.cfg.queues.max_pending_frames + 2)
            budget = self.cfg.queues.decoded_buffer_budget_mib
            allocated = sum(rt.reserved_bytes for rt in self.runtimes.values())
            if budget is not None and allocated + reserved > budget * 1024 ** 2:
                return self._deny(row, response, "capacity_limit")
            if worker:
                self.pool.claim(worker, task_id)  # Reserve atomically, before receiver setup awaits.
            else:
                self.waiting.append(task_id)  # Preparation still counts until execution-ready.
            frames = FrameQueue(self.cfg.queues.max_pending_frames, symbol(self.cfg.queues.frame_policy)(),
                                lambda f, reason: self.loop.call_soon_threadsafe(self.drop_frame, task_id, f.record(), reason),
                                can_drop_frames=request.can_drop_frames)
            rt = Runtime(row, app, frames, worker=worker, reserved_bytes=reserved)
            self.runtimes[task_id] = rt
            row.config, row.priority = app.model_dump(), plan.priority
            row.times["receiver_setup_start"] = stamp()
            receiver = Receiver(task_id, app, self.cfg.media, frames, self.log)
            try:
                endpoint = await asyncio.to_thread(receiver.start, self.cfg.network, self.cfg.media)
            except Exception as exc:
                self.log.event("receiver_setup_failed", task_id=task_id, detail=repr(exc))
                await asyncio.to_thread(receiver.stop)
                self._remove_waiting(task_id)
                if worker:
                    self.pool.reset(worker)
                del self.runtimes[task_id]
                return self._deny(row, response, "receiver_setup_failed")
            rt.receiver = receiver
            row.times["receiver_setup_end"] = stamp()
            row.accepted, row.endpoint = True, endpoint
            row.times["accepted"] = stamp()
            row.status = "awaiting_delivery" if worker else "waiting_resources"
            if worker:
                row.worker_id = worker.worker_id
                row.times["execution_ready"] = stamp()
                row.times["preparation_start"] = row.times["execution_ready"]
                row.times["preparation_finish"] = row.times["execution_ready"]
                self.log.event("worker_assigned", task_id=task_id, worker_id=worker.worker_id, warm=True)
            ue.active_task_ids.append(task_id)
            ue.activity = "active"
            response.update(accepted=True, status="preparing", task_type=request.task_type,
                app_config=app.model_dump(), task_priority=plan.priority, data_plane=endpoint,
                accuracy_verification="profile_deferred" if app.accuracy_profile is None else "not_certified_by_baseline",
                queue_policy={"max_pending_frames": self.cfg.queues.max_pending_frames,
                              "dispatch": "fifo" if self.cfg.queues.frame_policy == "mec.frames:FIFO" else self.cfg.queues.frame_policy,
                              "overflow": "drop_oldest",
                              "overflow_overrides_can_drop_frames": True,
                              "can_drop": request.can_drop, "can_drop_frames": request.can_drop_frames})
            row.response = copy.deepcopy(response)
            self.log.event("task_accepted", task_id=task_id, decision_source=source, priority=plan.priority)
            return response

    def _deny(self, row, response, reason):
        row.status, row.reason, row.terminal = "denied", reason, True
        row.times["terminal_recorded_at"] = stamp()
        response["reason"] = reason
        row.response = copy.deepcopy(response)
        self.log.event("task_denied", task_id=row.task_id, reason=reason)
        return response

    def _remove_waiting(self, task_id):
        try:
            self.waiting.remove(task_id)
        except ValueError:
            pass

    def task_end(self, ue_id, task_id, final_frame_id):
        rt = self.owned(ue_id, task_id)
        if not isinstance(final_frame_id, int) or isinstance(final_frame_id, bool) or final_frame_id < -1:
            raise ValueError("final_frame_id must be an integer >= -1; frames begin at zero")
        if rt.end_received_ns is not None:
            if rt.frames.final_frame_id != final_frame_id:
                raise ValueError("Conflicting final frame ID")
            return
        rt.frames.final_frame_id = final_frame_id
        rt.end_received_ns = time.monotonic_ns()
        rt.row.times["task_end_receive"] = stamp()
        self.log.event("task_end", task_id=task_id, final_frame_id=final_frame_id)

    def owned(self, ue_id, task_id):
        row = self.tasks.get(task_id)
        if row is None or row.ue_id != ue_id:
            raise ValueError("Unknown task for this UE")
        rt = self.runtimes.get(task_id)
        if not rt or row.terminal:
            raise ValueError("Task is terminal")
        return rt

    def terminate(self, task_id, status, reason):
        row = self.tasks[task_id]
        if row.terminal:
            return
        row.status, row.reason, row.terminal = status, reason, True
        row.times["terminal_recorded_at"] = stamp()
        self._remove_waiting(task_id)
        rt = self.runtimes[task_id]
        rt.frames.discard(reason)
        if rt.receiver:
            rt.stop_task = self.background_task(asyncio.to_thread(rt.receiver.stop))
        ue = self.ues[row.ue_id]
        if task_id in ue.active_task_ids:
            ue.active_task_ids.remove(task_id)
        ue.activity = "active" if ue.active_task_ids else "idle"
        self.log.event("task_terminal", task_id=task_id, outcome=status, reason=reason,
                       terminal_recorded_at=row.times["terminal_recorded_at"])
        self.hub.emit(ue.ue_id, {"type": {"expired": "task_expired", "failed": "task_failed",
                      "completed": "task_completed", "cancelled": "task_cancelled"}[status],
                      "ue_id": ue.ue_id, "task_id": task_id, "status": status, "reason": reason,
                      "terminal_recorded_at": row.times["terminal_recorded_at"]})
        # The in-flight job is NOT interrupted; its worker is released only when
        # completion arrives. Terminal task state cannot be resurrected then.

    def drop_frame(self, task_id, frame, reason):
        row = self.tasks[task_id]
        metadata = frame if isinstance(frame, dict) else frame.record()
        record = {**metadata, "task_id": task_id, "ue_id": row.ue_id,
                  "outcome": "dropped", "reason": reason, "terminal_recorded_at": stamp(),
                  "priority": row.priority, "config_id": row.config["config_id"] if row.config else None,
                  "slo_met": False}
        row.counts["dropped"] += 1
        self.record_job(record)
        self.hub.emit(row.ue_id, {"type": "frame_result", "ue_id": row.ue_id, "task_id": task_id,
                      "frame_id": metadata["frame_id"], "job_seq": metadata["job_seq"], "status": "dropped",
                      "reason": reason, "timing": record, "payload": None})

    def record_job(self, record):
        self.recent_jobs.append(record)
        self.log.job(record)

    def _finished_job(self, worker, message):
        rt = self.runtimes.get(worker.task_id)
        if not rt or rt.in_flight is None:
            return
        frame, rt.in_flight = rt.in_flight, None
        worker.in_flight = False
        row = rt.row
        completed = message["type"] == "job_done"
        times = {**frame.times, **message.get("times", {})}
        record = {**frame.record(), "times": times, "task_id": row.task_id, "ue_id": row.ue_id,
                  "outcome": "completed" if completed else "failed", "reason": None if completed else "execution_failed",
                  "terminal_recorded_at": stamp(), "config_id": rt.app.config_id,
                  "priority": row.priority, "priority_integer": message.get("priority_integer"),
                  "cuda_event_ms": message.get("cuda_event_ms"),
                  "gpu_start_time": times.get("gpu_submit"),
                  "gpu_finish_time": times.get("gpu_complete"),
                  "gpu_time_definition": "host submission / observed completion, not kernel start/end",
                  # MEC cannot certify capture-to-UE-receipt before UE logs receipt.
                  "slo_met": None if completed else False,
                  "result_metadata": {"payload_keys": list(message.get("payload", {}))}}
        if "processing_start" in times:
            queue_ms = (times["processing_start"]["mono_ns"] - times["queue_enter"]["mono_ns"]) / 1e6
            processing_ms = (times["processing_finish"]["mono_ns"] - times["processing_start"]["mono_ns"]) / 1e6
            record["queue_ms"], record["processing_ms"] = queue_ms, processing_ms
            row.total_queue_ms += queue_ms
            row.total_processing_ms += processing_ms
            row.times.setdefault("first_processing_start", times["processing_start"])
            row.times["last_processing_finish"] = times["processing_finish"]
            row.times.setdefault("queue_enter_time", times["queue_enter"])
            if times.get("gpu_submit"):
                row.times.setdefault("gpu_start_time", times["gpu_submit"])
                row.times["gpu_finish_time"] = times["gpu_complete"]
        if "frame_first_packet_receive" in times:
            record["reception_to_queue_ms"] = (times["queue_enter"]["mono_ns"] - times["frame_first_packet_receive"]["mono_ns"]) / 1e6
        row.counts["completed" if completed else "failed"] += 1
        self.record_job(record)
        self.hub.emit(row.ue_id, {"type": "frame_result", "ue_id": row.ue_id, "task_id": row.task_id,
            "frame_id": frame.frame_id, "job_seq": frame.job_seq,
            "status": record["outcome"], "reason": record["reason"], "task_type": rt.app.task_type,
            "timing": record, "payload": message.get("payload") if completed else None})
        if not completed:
            self.log.event("inference_failed", task_id=row.task_id, detail=message.get("detail"))
            self.terminate(row.task_id, "failed", "execution_failed")

    def _worker_message(self, worker, message):
        typ = message["type"]
        if typ == "ready":
            self.log.event("worker_ready", worker_id=worker.worker_id, **{k: v for k, v in message.items() if k != "type"})
            rt = self.runtimes.get(worker.task_id)
            if rt and not rt.row.terminal:
                ready_at = message["time"]["mono_ns"]
                prep = rt.row.times.get("preparation_start", {}).get("mono_ns", worker.created_mono_ns)
                accepted = rt.row.times["accepted"]["mono_ns"]
                if ready_at > accepted + self.cfg.timeouts.execution_ready_s * 1e9:
                    self.terminate(rt.row.task_id, "expired", "readiness_timeout")
                elif ready_at > prep + self.cfg.timeouts.preparation_s * 1e9:
                    self.terminate(rt.row.task_id, "failed", "preparation_timeout")
            if rt and not rt.row.terminal:
                worker.state = "assigned"
                rt.row.times["preparation_finish"] = stamp()
                rt.row.times["execution_ready"] = stamp()
                rt.row.status = "draining" if rt.input_closed_ns else "awaiting_delivery"
                self._remove_waiting(rt.row.task_id)
            else:
                self.pool.reset(worker)
        elif typ == "reset_done":
            worker.state, worker.idle_mono_ns = "idle", time.monotonic_ns()
        elif typ in {"job_done", "job_failed"}:
            self._finished_job(worker, message)
        elif typ == "worker_failed":
            rt = self.runtimes.get(worker.task_id)
            self.log.event("worker_failed", worker_id=worker.worker_id, detail=message.get("detail"))
            if rt:
                if rt.in_flight:
                    self._finished_job(worker, {"type": "job_failed", "detail": message.get("detail")})
                self.terminate(rt.row.task_id, "failed", "preparation_failed" if worker.state == "preparing" else "worker_failed")
                rt.worker = None
            self.pool.retire(worker)

    def _assign_idle_workers(self):
        for task_id in list(self.waiting):
            rt = self.runtimes[task_id]
            if not rt.row.accepted or rt.row.terminal or rt.worker:
                continue
            worker = self.pool.idle(rt.app.config_id)
            if worker:
                self.pool.claim(worker, task_id)
                rt.worker = worker
                rt.row.worker_id = worker.worker_id
                rt.row.times["execution_ready"] = stamp()
                rt.row.status = "draining" if rt.input_closed_ns else "awaiting_delivery"
                self._remove_waiting(task_id)
                self.log.event("worker_assigned", task_id=task_id, worker_id=worker.worker_id, warm=True)

    def _assign_workers(self):
        # First assign compatible warm workers across the entire FIFO. Only then
        # evict remaining idle workers to prepare incompatible queued models.
        self._assign_idle_workers()
        for task_id in list(self.waiting):
            rt = self.runtimes[task_id]
            if not rt.row.accepted or rt.row.terminal or rt.worker:
                continue
            self.pool.make_space(rt.app)
            worker = self.pool.prepare(rt.app, task_id)
            if worker:
                rt.worker = worker
                rt.row.worker_id = worker.worker_id
                rt.row.status = "preparing"
                rt.row.times["preparation_start"] = stamp()
                self.log.event("preparation_started", task_id=task_id, worker_id=worker.worker_id, warm=False)
        # Task requests get first access to capacity; optional prewarming uses idle capacity.
        if self.initial_replicas_pending:
            app = self.catalog.get(self.initial_replicas_pending[0])
            if self.pool.prepare(app):
                self.initial_replicas_pending.popleft()

    def _deadlines(self, rt, now):
        row, timeout = rt.row, self.cfg.timeouts
        accepted = row.times["accepted"]["mono_ns"]
        if rt.receiver.first_media is None and now - accepted >= timeout.first_media_s * 1e9:
            self.terminate(row.task_id, "expired", "no_input_timeout")
            return
        if "execution_ready" not in row.times:
            if now - accepted >= timeout.execution_ready_s * 1e9:
                self.terminate(row.task_id, "expired", "readiness_timeout")
                return
            if rt.worker is None and now - accepted >= timeout.worker_wait_s * 1e9:
                self.terminate(row.task_id, "expired", "resource_timeout")
                return
            if "preparation_start" in row.times and now - row.times["preparation_start"]["mono_ns"] >= timeout.preparation_s * 1e9:
                self.terminate(row.task_id, "failed", "preparation_timeout")
                return
        if rt.end_received_ns is not None and rt.input_closed_ns is None:
            final = rt.frames.final_frame_id
            with rt.frames.lock:
                missing = list(rt.frames.received.missing(final))
            if not missing or now - rt.end_received_ns >= timeout.final_frame_arrival_grace_s * 1e9:
                rt.input_closed_ns = now
                rt.frames.closed = True
                row.status = "draining"
                row.times["input_closed"] = stamp()
                rt.stop_task = self.background_task(asyncio.to_thread(rt.receiver.stop))
                for lo, hi in missing:
                    # A fully lost frame has no recoverable capture metadata.
                    # Log ranges rather than fabricating per-frame timestamps.
                    self.log.event("frames_missing", task_id=row.task_id, frame_id_first=lo, frame_id_last=hi,
                                   loss_detected_at=stamp(), outcome="lost")
                    self.hub.emit(row.ue_id, {"type": "frames_missing", "ue_id": row.ue_id,
                        "task_id": row.task_id, "frame_id_first": lo, "frame_id_last": hi,
                        "status": "lost", "reason": "arrival_grace_expired"})
                row.counts["lost"] = sum(hi - lo + 1 for lo, hi in missing)
        if rt.input_closed_ns is not None:
            if now - rt.input_closed_ns >= timeout.drain_s * 1e9 and (len(rt.frames) or rt.in_flight or "execution_ready" not in row.times):
                self.terminate(row.task_id, "expired", "drain_timeout")
            elif len(rt.frames) == 0 and rt.in_flight is None:
                self.terminate(row.task_id, "completed", "completed_with_missing_frames" if row.counts["lost"] else None)

    async def tick(self):
        now = time.monotonic_ns()
        for worker, message in self.pool.poll():
            self._worker_message(worker, message)
        for ue in self.ues.values():
            if ue.connection_state == "disconnected" and ue.disconnected_mono_ns is not None:
                if now - ue.disconnected_mono_ns >= self.cfg.timeouts.disconnect_grace_s * 1e9:
                    for task_id in list(ue.active_task_ids):
                        self.terminate(task_id, "expired", "control_disconnect_timeout")
        for rt in list(self.runtimes.values()):
            if rt.row.accepted and not rt.row.terminal:
                if rt.receiver.error:
                    self.terminate(rt.row.task_id, "failed", "media_failed")
                    continue
                self._deadlines(rt, now)
        self._assign_workers()
        for task_id, rt in list(self.runtimes.items()):
            if rt.row.terminal:
                if rt.in_flight is None:
                    if rt.worker and rt.worker.state == "assigned":
                        self.pool.reset(rt.worker)
                        rt.worker = None
                    # Preparation that finished for an expired task is handled by ready.
                    if rt.worker and rt.worker.state == "preparing":
                        self.pool.retire(rt.worker)
                        rt.worker = None
                    if rt.stop_task is None or rt.stop_task.done():
                        del self.runtimes[task_id]
                continue
            if rt.worker and rt.worker.state == "assigned" and rt.in_flight is None:
                frame = rt.frames.pop()
                if frame:
                    rt.in_flight = frame
                    rt.row.status = "draining" if rt.input_closed_ns else "active"
                    frame.times["dispatch"] = stamp()
                    self.pool.submit(rt.worker, frame, rt.row.request["parameters"], rt.row.priority)
        self.pool.evict_idle(now)
        if now - self.snapshot_ns >= self.cfg.snapshot_interval_s * 1e9:
            self.snapshot()
            self.snapshot_ns = now
        if self.log.error:
            raise RuntimeError(f"Logging failed: {self.log.error}")

    def snapshot(self, stopped=False):
        outstanding = []
        for rt in self.runtimes.values():
            outstanding.extend({"task_id": rt.row.task_id, **r} for r in rt.frames.snapshot())
            if rt.in_flight:
                outstanding.append({"task_id": rt.row.task_id, "in_flight": True, **rt.in_flight.record()})
        self.log.snapshot({"started": self.started, "snapshot_time": stamp(), "stopped": stopped,
            "ended": stamp() if stopped else None, "software_versions": self.versions,
            "configuration": self.cfg.model_dump(), "catalog": self.catalog.model_dump(),
            "ues": [asdict(u) for u in self.ues.values()], "tasks": [r.row() for r in self.tasks.values()],
            "execution_registry": outstanding, "recent_jobs": list(self.recent_jobs), "pool": self.summary(),
            "flush_policy": "JSONL line flush; atomic fsynced snapshots; background writer"})

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.running = True
        self.background_task(self.telemetry.run(self.log))
        while self.running:
            await self.tick()
            await asyncio.sleep(self.cfg.tick_s)

    async def close(self):
        self.running = False
        # Snapshot BEFORE teardown preserves current outstanding work on Ctrl+C.
        self.snapshot(stopped=True)
        for rt in list(self.runtimes.values()):
            if rt.receiver:
                await asyncio.to_thread(rt.receiver.stop)
        deadline = time.monotonic() + self.cfg.pool.shutdown_grace_s
        while any(w.in_flight for w in self.pool.workers.values()) and time.monotonic() < deadline:
            for worker, message in self.pool.poll():
                self._worker_message(worker, message)
            await asyncio.sleep(0.02)
        for worker in list(self.pool.workers.values()):
            self.pool.remove(worker)
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*list(self.background), return_exceptions=True)
        self.snapshot(stopped=True)
        await asyncio.to_thread(self.log.close)
