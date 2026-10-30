from __future__ import annotations
import asyncio
import hashlib
import json
import math
import queue
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from .config import collection_candidates, qualified
from .statistics import History, NetworkFilter
from .policies import HeuristicGoP, HeuristicPriority, validate_joint
from .recording import Recorder, ReplayStore
from .media import Receiver, MockReceiver
from .workers import Workers
from .tracking import Tracker
from .gpu import DeviceGpuSampler
from mec_contracts.rpc import Client
from mec_contracts.wire import envelope, result_id, integer, dumps

TERMINAL = {"DENIED", "COMPLETED", "FAILED", "CANCELLED", "EXPIRED"}


class Ranges:
    def __init__(self):
        self.ranges = []

    def has(self, fid):
        return any(a <= fid <= b for a, b in self.ranges)

    def add(self, fid):
        values = sorted(self.ranges + [(fid, fid)])
        out = []
        for a, b in values:
            if out and a <= out[-1][1] + 1:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        self.ranges = out


@dataclass
class UE:
    uid: str
    token_hash: str
    connection_id: str = ""
    ip: str = ""
    peer_ip: str = ""
    connected: bool = False
    active_video: str | None = None
    prepared_video: str | None = None
    sender: object = None
    generation: int = 0
    association_revision: int = 0
    disconnect_at: float | None = None
    network: object = None
    clock_uncertainty_ns: int | None = None
    clock_mapping: dict = field(default_factory=dict)
    alias: str | None = None
    scenario_notice: tuple | None = None
    # The control socket is only considered usable for an attempt after the UE
    # confirms that it received that attempt's immutable assignment.
    scenario_ack: tuple | None = None


@dataclass
class Video:
    vid: str
    uid: str
    app: str
    epoch: int
    admitted: float
    state: str = "AWAITING_DECISION"
    receiver: object = None
    port: int = 0
    slo: float = 0.15
    can_drop: bool = False
    can_drop_frames: bool = False
    configs: dict = field(default_factory=dict)
    plans: dict = field(default_factory=dict)
    current: dict | None = None
    pending: dict | None = None
    applied: dict | None = None
    received: int = 0
    final_id: int | None = None
    input_close_at: float | None = None
    drain_until: float | None = None
    input_closed: bool = False
    suspension_boundary: int | None = None
    suspension_version: int = 0
    control_version: int = 0
    transmission_control: dict | None = None
    last_descriptor_id: int = -1
    suspension_gap_version: int = 0
    finalized: bool = False
    deliveries: int = 0
    collection_error: str | None = None
    requested_candidate_id: str | None = None
    # A prerecorded successor has transport/configuration ready before its handoff.
    predecessor_id: str | None = None
    expected_start: float | None = None
    demand_closed: bool = False
    gap_scan: int = 0
    resolved: object = field(default_factory=Ranges)
    radio_frames: dict = field(default_factory=dict)
    tracker: object = None
    tracker_busy: bool = False
    tracking_ready: dict = field(default_factory=dict)
    counters: dict = field(
        default_factory=lambda: dict(completed=0, dropped=0, failed=0)
    )


@dataclass
class Frame:
    vid: str
    fid: int
    descriptor: object
    first_ns: int
    config: dict
    state: str = "RECEIVING"
    complete_ns: int | None = None
    decoded_ns: int | None = None
    queue_ns: int | None = None
    priority: int | None = None
    network_s: float = 0.0
    deadline_ns: int | None = None
    rgb: object = None
    times: dict = field(default_factory=dict)
    payload: dict | None = None
    detection_ready_ns: int | None = None


# The asyncio loop owns registries; receiver threads and workers report events.
class Engine:
    def __init__(self, cfg, catalog, profiles, workers=None):
        self.cfg = cfg
        self.catalog = catalog
        self.profiles = profiles
        eligible = (
            collection_candidates(catalog, profiles)
            if cfg.mode == "collection"
            else qualified(catalog, profiles)
        )
        self.candidates = {
            cid: c
            for cid, c in eligible.items()
            if c["application"] in cfg.applications
        }
        self.root = Path(cfg.runs_dir) / cfg.run_id / "mec"
        self.scenario = None
        self.scenario_assignments = {}
        self.scenario_videos = {}
        # Aetheredge: revisions restart at zero with every MEC process. This
        # generation lets EdgeRIC distinguish a restart from delayed messages.
        self.awareness_generation = str(uuid.uuid4())
        self.record = Recorder(self.root, self.join_context)
        self.gpu = DeviceGpuSampler()
        self.replay = ReplayStore(self.root / "replay.sqlite3")
        self.ues = {}
        self.tokens = {}
        self.videos = {}
        self.frames = {}
        self.inflight = {}
        self.dedup = {}
        self.quarantined = set()
        self.pending = {
            (a, p): deque()
            for a in cfg.applications
            for p in cfg.priority_streams
        }
        self.histories = {}
        self.residence = {}
        self.queue_history = History(cfg.global_queue_initial_s or 0.0)
        self.policy = HeuristicGoP()
        self.priority = HeuristicPriority(cfg.priority_streams)
        self.plan_version = 0
        self.config_version = 0
        self.last_members = ()
        self.last_cycle = -1
        self.epoch_unix_ns = time.time_ns() // 1_000_000_000 * 1_000_000_000
        self.last_report = None
        self.last_usable = {}
        self.report_received = 0.0
        self.rpc = Client(cfg.edgeric_target)
        self.awareness = asyncio.PriorityQueue(maxsize=4096)
        self.awareness_seq = 0
        self.synced_awareness_revision = 0
        self.sync_needed = True
        self.progress = {}
        self.last_progress = {}
        self.worker_failures = {}
        self.closed = False
        self.loop = None
        self.events = queue.Queue(maxsize=1024)
        self.media_bridge_errors = 0
        self.admission_lock = asyncio.Lock()
        self.background = []
        self.last_snapshot = 0.0
        self.workers = workers or Workers(
            catalog,
            dict(
                backend=cfg.backend,
                weights_dir=cfg.weights_dir,
                warmup_count=cfg.warmup_count,
                applications=cfg.applications,
                priority_streams=cfg.priority_streams,
            ),
        )
        for cid, c in self.candidates.items():
            shared = next(
                (
                    value
                    for prior, value in self.histories.items()
                    if all(
                        self.candidates[prior][k] == c[k]
                        for k in ("application", "model", "input_hw", "precision")
                    )
                ),
                None,
            )
            self.histories[cid] = shared or (
                History(c["processing_s"]),
                History(c.get("tracker_s", 0.0)),
            )
        self.record.event(
            "configuration",
            settings=cfg.model_dump(),
            catalog=catalog,
            profiles=profiles,
        )

    @property
    def collection_mode(self):
        return getattr(self.cfg, "mode", None) == "collection"

    def join_context(self):
        value = dict(
            run_id=self.cfg.run_id,
            campaign_id=None,
            scenario_id=None,
            attempt_id=None,
        )
        if self.scenario:
            value.update(self.scenario)
        return value

    def contextual(self, message):
        """Attach the active immutable join context to a wire message."""
        if message is None:
            return None
        for key, value in self.join_context().items():
            message.setdefault(key, value)
        return message

    @staticmethod
    def validated_context(value):
        context = {}
        for key in ("campaign_id", "scenario_id", "attempt_id"):
            item = value.get(key)
            if not isinstance(item, str) or not item.strip() or len(item) > 128:
                raise ValueError(f"invalid {key}")
            context[key] = item
        return context

    def idle(self):
        return bool(
            not self.frames
            and not self.inflight
            and all(not values for values in self.pending.values())
            and all(
                v.state in TERMINAL and v.deliveries == 0
                for v in self.videos.values()
            )
        )

    def arm_scenario(self, value):
        context = self.validated_context(value)
        assignments = {}
        for item in value.get("ues", []):
            alias = item.get("ue_alias")
            candidate = item.get("candidate_id")
            if not isinstance(alias, str) or not alias or alias in assignments:
                raise ValueError("invalid or duplicate ue_alias")
            if candidate not in self.candidates:
                raise ValueError(f"unknown candidate for {alias}")
            if self.candidates[candidate]["application"] != item.get("application"):
                raise ValueError(f"candidate application mismatch for {alias}")
            if int(self.candidates[candidate]["bitrate_bps"]) != int(
                item.get("bitrate_bps", -1)
            ):
                raise ValueError(f"candidate bitrate mismatch for {alias}")
            frame_count = integer(item.get("frame_count"), 1)
            source_start = integer(item.get("source_start_frame"), 0)
            assignments[alias] = dict(
                item, frame_count=frame_count, source_start_frame=source_start
            )
        if self.scenario == context and self.scenario_assignments == assignments:
            return dict(status="already_armed", **context)
        if not self.idle():
            raise ValueError("MEC must be idle before arming a scenario")
        self.scenario = context
        self.scenario_assignments = assignments
        self.scenario_videos.clear()
        self.videos.clear()
        self.progress.clear()
        self.last_progress.clear()
        self.last_report = None
        self.report_received = 0.0
        self.last_usable.clear()
        self.sync_needed = True
        # Wake a sender blocked on an empty awareness queue. The subsequent
        # authoritative sync subsumes this fence, so it is never sent.
        self.awareness_seq += 1
        fence = self.contextual(
            envelope(
                "scenario_fence",
                self.cfg.run_id,
                awareness_generation=self.awareness_generation,
                awareness_revision=self.awareness_seq,
            )
        )
        try:
            self.awareness.put_nowait((0, self.awareness_seq, fence))
        except asyncio.QueueFull:
            pass
        self.record.event("scenario_armed")
        for ue in tuple(self.ues.values()):
            ue.scenario_ack = None
            self.send_scenario_context(ue)
        return dict(status="armed", assignments=len(assignments), **context)

    def disarm_scenario(self, value):
        """Clear one fully drained attempt without restarting resident workers."""
        context = self.validated_context(value)
        if self.scenario is None:
            return dict(status="already_disarmed", **context)
        if self.scenario != context:
            raise ValueError("scenario_context_mismatch")
        if not self.idle():
            raise ValueError("MEC must be idle before disarming a scenario")
        # Record under the attempt being retired, then remove its authority so
        # a UE reconnect cannot receive and restart the completed assignment.
        self.record.event("scenario_disarmed")
        self.scenario = None
        self.scenario_assignments.clear()
        self.scenario_videos.clear()
        for ue in self.ues.values():
            ue.scenario_notice = None
            ue.scenario_ack = None
        return dict(status="disarmed", **context)

    def send_scenario_context(self, ue):
        """Send one assignment per attempt and control-connection generation."""
        if not self.scenario:
            return
        notice = (self.scenario["attempt_id"], ue.generation, ue.alias)
        if ue.scenario_notice == notice:
            return
        assignment = self.scenario_assignments.get(ue.alias)
        self.send(
            ue.uid,
            envelope(
                "scenario_context",
                self.cfg.run_id,
                active=assignment is not None,
                assignment=assignment,
            ),
        )
        ue.scenario_notice = notice

    def validate_message_context(self, message):
        if not self.scenario:
            if self.collection_mode:
                raise ValueError("scenario_not_armed")
            return
        actual = {key: message.get(key) for key in self.scenario}
        if actual != self.scenario:
            raise ValueError("scenario_context_mismatch")

    def task(self, coro):
        t = asyncio.create_task(coro)
        self.background.append(t)

        def done(task):
            if task in self.background:
                self.background.remove(task)
            if not task.cancelled() and task.exception():
                self.record.event("background_error", reason=repr(task.exception()))

        t.add_done_callback(done)
        return t

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.workers.start()
        self.tasks = [
            asyncio.create_task(self.tick()),
            asyncio.create_task(self.send_awareness()),
            asyncio.create_task(self.receive_reports()),
            asyncio.create_task(self.resource_reports()),
        ]

    def post(self, kind, data):
        # Bounded staging. A decoded overflow is made an explicit outcome on the loop.
        try:
            self.events.put_nowait((kind, data))
        except queue.Full:
            self.media_bridge_errors += 1
            if kind == "decoded":
                d = data["descriptor"]
                self.loop.call_soon_threadsafe(
                    self.fail_frame,
                    d.video_id,
                    d.frame_id,
                    "collection_event_overflow"
                    if self.collection_mode
                    else "buffer_overflow",
                )

    def send(self, uid, message):
        u = self.ues[uid]
        if u.connected and u.sender:
            u.sender(self.contextual(message))

    def aware(self, kind, priority=1, **value):
        self.awareness_seq += 1
        # The creation revision lets a later authoritative sync fence queued stale updates.
        msg = envelope(
            kind,
            self.cfg.run_id,
            awareness_generation=self.awareness_generation,
            awareness_revision=self.awareness_seq,
            **value,
        )
        self.contextual(msg)
        try:
            self.awareness.put_nowait((priority, self.awareness_seq, msg))
        except asyncio.QueueFull:
            self.sync_needed = True
            self.record.event("awareness_overflow", message_type=kind)

    def association(self, u):
        u.association_revision += 1
        self.aware(
            "association",
            0,
            ue_id=u.uid,
            connection_id=u.connection_id,
            ip=u.ip,
            connection_generation=u.generation,
            association_revision=u.association_revision,
        )

    # A reconnect replaces control ownership while retaining the ongoing video.
    def hello(self, resume_token, peer_ip, sender):
        hashed = (
            hashlib.sha256(resume_token.encode()).hexdigest() if resume_token else ""
        )
        uid = self.tokens.get(hashed)
        token = resume_token
        if uid is None:
            uid = str(uuid.uuid4())
            token = secrets.token_urlsafe(32)
            hashed = hashlib.sha256(token.encode()).hexdigest()
            u = UE(uid, hashed)
            self.ues[uid] = u
            self.tokens[hashed] = uid
        u = self.ues[uid]
        old = u.sender
        self.last_usable.pop(uid, None)
        u.generation += 1
        u.connection_id = str(uuid.uuid4())
        u.connected = True
        u.disconnect_at = None
        u.peer_ip = peer_ip
        u.sender = sender
        if old:
            old(envelope("connection_replaced", self.cfg.run_id, ue_id=uid))
        # Fixed collection records raw radio evidence without using a network predictor.
        u.network = (
            None
            if self.collection_mode
            else NetworkFilter(self.profiles["network"])
        )
        if u.network:
            u.network.reset(time.monotonic())
        self.association(u)
        self.record.event(
            "connected",
            ue_id=uid,
            connection_id=u.connection_id,
            generation=u.generation,
        )
        return u, self.contextual(envelope(
            "mec_hello",
            self.cfg.run_id,
            ue_id=uid,
            resume_token=token,
            connection_id=u.connection_id,
            catalog_version=self.catalog["version"],
            supported_configurations=list(self.candidates.values()),
            input_profile=dict(
                width=self.catalog["source_hw"][1],
                height=self.catalog["source_hw"][0],
                fps=self.cfg.fps,
                gop_frames=self.cfg.gop_frames,
            ),
            active_video_id=u.active_video,
            prepared_video_id=u.prepared_video,
            successor_prepare_s=self.cfg.successor_prepare_s,
            collection_mode=self.collection_mode,
            scenario_active=(
                bool(self.scenario)
                and u.alias in self.scenario_assignments
            ),
            assignment=self.scenario_assignments.get(u.alias),
            server_unix_ns=time.time_ns(),
            schema_version_control=1,
        ))

    def disconnect(self, uid, generation):
        u = self.ues[uid]
        if u.generation != generation:
            return
        u.connected = False
        u.sender = None
        u.disconnect_at = time.monotonic()
        self.record.event("disconnected", ue_id=uid, connection_id=u.connection_id)

    # Reserve each lifecycle command before awaiting setup so retries share one outcome.
    async def command(self, u, m):
        kind = m.get("type")
        if kind == "time_sync":
            return envelope(
                "time_sync",
                self.cfg.run_id,
                client_mono_ns=m["client_mono_ns"],
                server_receive_unix_ns=time.time_ns(),
                server_send_unix_ns=time.time_ns(),
            )
        if kind == "association_update":
            import ipaddress

            u.ip = str(ipaddress.ip_address(m["ue_ip"]))
            alias = m.get("ue_alias")
            if alias is not None:
                if not isinstance(alias, str) or not alias.strip() or len(alias) > 64:
                    raise ValueError("invalid ue_alias")
                if any(x.uid != u.uid and x.alias == alias for x in self.ues.values()):
                    raise ValueError("ue_alias already connected")
                u.alias = alias
            self.association(u)
            u.clock_uncertainty_ns = m.get("clock_uncertainty_ns")
            u.clock_mapping = m.get("clock_mapping", {})
            if self.scenario:
                self.send_scenario_context(u)
            return envelope("association_pending", self.cfg.run_id, ue_id=u.uid)
        if kind == "scenario_context_ack":
            self.validate_message_context(m)
            if u.alias not in self.scenario_assignments:
                raise ValueError("UE has no assignment in active scenario")
            u.scenario_ack = (self.scenario["attempt_id"], u.generation, u.alias)
            self.record.event(
                "scenario_context_acknowledged",
                ue_id=u.uid,
                ue_alias=u.alias,
                connection_id=u.connection_id,
            )
            return envelope(
                "scenario_context_acknowledged", self.cfg.run_id, ue_id=u.uid
            )
        if kind == "probe_report":
            self.record.event("ue_probe_report", ue_id=u.uid, report=m)
            return envelope("probe_report_recorded", self.cfg.run_id, ue_id=u.uid)
        if kind == "plan_unapplied":
            self.record.event("plan_unapplied", ue_id=u.uid, report=m)
            return None
        if kind not in ("video_start", "video_end"):
            raise ValueError("unknown control command")
        self.validate_message_context(m)
        if m.get("ue_id") != u.uid:
            raise ValueError("identity does not match socket")
        seq = integer(m["client_seq"])
        key = (u.uid, seq)
        payload = dumps(dict(sorted(m.items())))
        if key in self.dedup:
            old, future = self.dedup[key]
            if old != payload:
                return envelope(
                    "error",
                    self.cfg.run_id,
                    reason="conflicting_client_seq",
                    client_seq=seq,
                )
            return await asyncio.shield(future)
        future = asyncio.get_running_loop().create_future()
        self.dedup[key] = (payload, future)
        try:
            result = (
                await self.start_video(u, m)
                if kind == "video_start"
                else self.end_video(u, m)
            )
        except Exception as ex:
            self.record.event("command_error", ue_id=u.uid, reason=repr(ex))
            result = envelope(
                "error", self.cfg.run_id, ue_id=u.uid, client_seq=seq, reason=str(ex)
            )
        future.set_result(result)
        return result

    def readiness(self):
        return dict(
            ready=not self.closed
            and set(self.workers.ready) == set(self.cfg.applications)
            and bool(self.candidates)
            and not self.worker_failures
            and not self.record.error,
            backend=self.cfg.backend,
            experimental=self.cfg.mode in ("experimental", "collection"),
            mode=self.cfg.mode,
            workers=self.workers.ready,
            worker_failures=self.worker_failures,
            recording_error=self.record.error,
            qualified_candidates=len(self.candidates),
        )

    # Admission may use a brief cached-report grace; ordinary freshness stays stricter.
    def usable_radio(self, uid, admission=False):
        if (self.cfg.mode == "development" or self.collection_mode):
            return True
        report = self.last_report
        if not report:
            return False
        radio = report.get("ues", {}).get(uid, {})
        identity_valid = bool(
            radio.get("association_valid")
            and radio.get("connection_id") == self.ues[uid].connection_id
        )
        fresh = bool(
            time.time_ns() - report["window_end_unix_ns"]
            <= self.cfg.ran_telemetry["stale_ms"] * 1_000_000
            and report.get("trace_events", 0) > 0
            and identity_valid
            and report.get("trace_coverage_valid")
        )
        if fresh:
            self.last_usable[uid] = (dict(radio), report["window_end_unix_ns"])
        if fresh:
            return True
        cached = self.last_usable.get(uid)
        return bool(
            admission
            and identity_valid
            and cached
            and time.time_ns() - cached[1]
            <= self.cfg.ran_telemetry["prolonged_outage_s"] * 1e9
        )

    def processing(self, cid):
        p, t = self.histories[cid]
        return p.value + (
            t.value if self.candidates[cid]["application"] == "tracking" else 0
        )

    @staticmethod
    def execution_key(candidate):
        return (
            candidate["application"],
            candidate["model"],
            tuple(candidate["input_hw"]),
            candidate["precision"],
        )

    # A model failure covers every bitrate entry backed by the same loaded model.
    def quarantine_candidate(self, cid, error):
        failed_key = self.execution_key(self.candidates[cid])
        affected = {
            candidate_id
            for candidate_id, candidate in self.candidates.items()
            if self.execution_key(candidate) == failed_key
        }
        self.quarantined.update(affected)
        self.record.event(
            "candidate_quarantined",
            candidate_id=cid,
            affected_candidates=sorted(affected),
            execution_key=list(failed_key),
            reason=error,
        )

    # Select one configuration per UE against shared radio and compute budgets.
    def choose(self, videos):
        decision_start = time.monotonic_ns()
        if self.collection_mode:
            proposal = {
                video.uid: (
                    video.requested_candidate_id
                    or self.cfg.fixed_candidates[video.app]
                )
                for video in videos
            }
            valid = all(
                candidate_id in self.candidates
                and candidate_id not in self.quarantined
                for candidate_id in proposal.values()
            )
            decision_end = time.monotonic_ns()
            self.record.event(
                "gop_decision",
                snapshot_id=str(uuid.uuid4()),
                decision_type="fixed_collection_candidate",
                policy_version="collection-v1",
                proposed=proposal,
                validated=valid,
                fallback_reason=None if valid else "fixed_candidate_unavailable",
                executed=proposal if valid else None,
                decision_start_monotonic_ns=decision_start,
                decision_end_monotonic_ns=decision_end,
                decision_latency_ns=decision_end - decision_start,
            )
            return proposal if valid else None
        us = {}
        cs = {
            cid: dict(c, processing_s=self.processing(cid))
            for cid, c in self.candidates.items()
            if cid not in self.quarantined
        }

        def finish(proposal, fallback_reason=None):
            validated = bool(
                proposal is not None
                and validate_joint(
                    proposal,
                    us,
                    cs,
                    self.cfg.radio_budget,
                    self.cfg.compute_budget_s_per_s,
                )
            )
            decision_end = time.monotonic_ns()
            self.record.event(
                "gop_decision",
                snapshot_id=str(uuid.uuid4()),
                decision_type="gop_bitrate_dnn",
                policy_version=self.policy.version,
                proposed=proposal,
                validated=validated,
                fallback_reason=fallback_reason,
                executed=proposal if validated else None,
                decision_start_monotonic_ns=decision_start,
                decision_end_monotonic_ns=decision_end,
                decision_latency_ns=decision_end - decision_start,
            )
            return proposal

        now = time.monotonic()
        for v in videos:
            u = self.ues[v.uid]
            ids = [cid for cid, c in cs.items() if c["application"] == v.app]
            if (
                self.cfg.mode != "development"
                and not self.usable_radio(v.uid)
                and (v.applied or v.pending)
            ):
                committed = (v.applied or v.pending)["candidate_id"]
                ids = [cid for cid in ids if cid == committed]
            if not ids:
                return finish(None, "no_usable_candidate")
            u.network.propagate(now)
            radio = (self.last_report or {}).get("ues", {}).get(u.uid, {})
            if not self.usable_radio(v.uid) and v.uid in self.last_usable:
                radio = self.last_usable[v.uid][0]
            units = radio.get("scheduled_prbs", 0)
            throughput = radio.get("throughput_bytes_s")
            # Explicit calibrated per-resource fallback for startup; never infer PRBs from stream count.
            rate = (
                throughput * 0.1 * 8 / units
                if throughput and units > 0
                else self.profiles["network"].get("radio_bps_per_unit")
            )
            if rate is None or rate <= 0:
                return finish(None, "invalid_radio_conversion")
            us[u.uid] = dict(
                candidate_ids=ids,
                slo_s=v.slo,
                mu_s=u.network.mu,
                variance_s2=u.network.variance(now),
                radio_bps_per_unit=rate,
                fps=self.cfg.fps,
            )
        proposal = self.policy.propose(
            us, cs, self.cfg.radio_budget, self.cfg.compute_budget_s_per_s
        )
        return finish(
            proposal,
            "heuristic_search_failed" if proposal is None else None,
        )

    # Hold admission serialization across receiver setup and recheck joint feasibility.
    async def start_video(self, u, m):
        async with self.admission_lock:
            decision_start = time.monotonic_ns()
            seq = m["client_seq"]
            vid = str(uuid.uuid4())

            def denied(reason):
                decision_end = time.monotonic_ns()
                # Keep rejected offered work visible to admission-rate analysis.
                self.record.event(
                    "admission_decision",
                    ue_id=u.uid,
                    video_id=vid,
                    client_seq=seq,
                    application=m.get("application"),
                    input=m.get("input"),
                    accepted=False,
                    reason=reason,
                    decision_start_monotonic_ns=decision_start,
                    decision_end_monotonic_ns=decision_end,
                    decision_latency_ns=decision_end - decision_start,
                )
                return envelope(
                    "video_response",
                    self.cfg.run_id,
                    ue_id=u.uid,
                    video_id=vid,
                    client_seq=seq,
                    accepted=False,
                    status="denied",
                    reason=reason,
                )

            predecessor_id = m.get("predecessor_video_id")
            if predecessor_id is not None:
                return await self.prepare_successor(
                    u, m, vid, decision_start, denied
                )

            if u.active_video and self.videos[u.active_video].state not in TERMINAL:
                return denied("active_video_exists")
            if not self.readiness()["ready"]:
                return denied("not_ready")
            if not self.usable_radio(u.uid, admission=True):
                return denied("usable_telemetry_required")
            if m["application"] not in self.cfg.applications:
                return denied("unsupported_application")
            assignment = None
            if self.collection_mode and self.scenario_assignments:
                assignment = self.scenario_assignments.get(u.alias)
                if assignment is None:
                    return denied("ue_not_active_in_scenario")
                # Aetheredge: one immutable assignment creates exactly one video
                # in an attempt, even if a terminal status prompts a client retry.
                if u.alias in self.scenario_videos:
                    return denied("scenario_already_started")
                for field in (
                    "application",
                    "candidate_id",
                    "media_id",
                    "source_start_frame",
                    "frame_count",
                ):
                    if m.get(field) != assignment.get(field):
                        return denied(f"scenario_assignment_mismatch:{field}")
            if (
                # Prepared successors and draining predecessors replace, rather than add,
                # sustained workloads; the independent port check bounds receivers.
                sum(
                    v.state in ("ACTIVE", "AWAITING_DELIVERY", "SUSPENDED")
                    for v in self.videos.values()
                )
                >= self.cfg.max_videos
            ):
                return denied("video_capacity")
            fmt = m.get("input", {})
            height, width = self.catalog["source_hw"]
            if (
                fmt.get("width", width),
                fmt.get("height", height),
                fmt.get("fps", self.cfg.fps),
            ) != (
                width,
                height,
                self.cfg.fps,
            ):
                return denied("unsupported_profiled_input")
            slo = float(m.get("slo_ms", self.cfg.slo_s * 1000)) / 1000
            if not math.isfinite(slo) or slo <= 0:
                return denied("invalid_slo")
            v = Video(
                vid,
                u.uid,
                m["application"],
                secrets.randbits(52) or 1,
                time.monotonic(),
                slo=slo,
                can_drop=bool(m.get("can_drop", False)),
                can_drop_frames=bool(m.get("can_drop_frames", False)),
                requested_candidate_id=(
                    assignment["candidate_id"] if assignment else None
                ),
            )
            self.videos[vid] = v
            u.active_video = vid
            participating = [
                x
                for x in self.videos.values()
                if x.state in ("ACTIVE", "AWAITING_DELIVERY")
            ] + [v]
            proposal = self.choose(participating)
            if proposal is None:
                v.state = "DENIED"
                u.active_video = None
                return denied("no_budget_feasible_assignment_found")
            used = {
                x.port
                for x in self.videos.values()
                if x.state not in TERMINAL and x.port
            }
            port = next(
                (
                    p
                    for p in range(
                        self.cfg.first_udp_port,
                        self.cfg.first_udp_port + self.cfg.max_videos * 2,
                    )
                    if p not in used
                ),
                None,
            )
            if port is None:
                v.state = "DENIED"
                u.active_video = None
                return denied("receiver_capacity")
            v.port = port
            receiver = (MockReceiver if self.cfg.backend == "mock" else Receiver)(
                vid, v.epoch, port, self.cfg, self.post
            )
            try:
                await asyncio.to_thread(receiver.start)
            except Exception as ex:
                await asyncio.to_thread(receiver.close)
                v.state = "DENIED"
                u.active_video = None
                self.record.event(
                    "receiver_setup_failed", video_id=vid, reason=repr(ex)
                )
                return denied("setup_failed")
            v.receiver = receiver
            v.state = "AWAITING_DELIVERY"
            v.admitted = time.monotonic()
            participating = [
                x
                for x in self.videos.values()
                if x.vid != v.vid and x.state in ("ACTIVE", "AWAITING_DELIVERY")
            ] + [v]
            proposal = self.choose(participating)
            if proposal is None:
                await asyncio.to_thread(receiver.close)
                v.state = "DENIED"
                u.active_video = None
                return denied("joint_budget_changed_during_setup")
            if v.app == "tracking":
                v.tracker = Tracker(
                    self.cfg.fps,
                    self.cfg.tracker_retention_s,
                    self.cfg.backend == "mock",
                )
            self.plan(participating, proposal)
            self.record.event(
                "admitted", ue_id=u.uid, video_id=vid, application=v.app, slo_s=v.slo
            )
            if assignment is not None:
                self.scenario_videos[u.alias] = vid
            decision_end = time.monotonic_ns()
            self.record.event(
                "admission_decision",
                ue_id=u.uid,
                video_id=vid,
                client_seq=seq,
                application=v.app,
                input=m.get("input"),
                accepted=True,
                reason=None,
                decision_start_monotonic_ns=decision_start,
                decision_end_monotonic_ns=decision_end,
                decision_latency_ns=decision_end - decision_start,
            )
            return self.video_response(u, v, seq)

    def video_response(self, u, v, seq):
        """Return the common transport contract for active and prepared videos."""
        return envelope(
            "video_response",
            self.cfg.run_id,
            ue_id=u.uid,
            client_seq=seq,
            video_id=v.vid,
            accepted=True,
            status="prepared" if v.state == "PREPARED" else "awaiting_delivery",
            media_epoch=v.epoch,
            endpoint=dict(host=self.cfg.advertised_ip, port=v.port),
            initial_frame_id=0,
            slo_ms=v.slo * 1000,
            latency_origin="transmission",
            config=v.pending,
            rtp=dict(extension_id=1, descriptor_bytes=96, mtu=1200, payload_type=96),
            queue_contract=dict(
                max_pending_frames=self.cfg.pending_frames,
                max_pending_bytes=self.cfg.pending_bytes_per_video,
                mandatory_drops=[]
                if self.collection_mode
                else [
                    "nonpositive_slack",
                    "deadline_expired",
                    "buffer_overflow",
                    "session_suspended",
                    "transport_loss",
                    "tracking_cutoff",
                ],
                hard_limit_invalidates_run=self.collection_mode,
            ),
            collection_mode=self.collection_mode,
        )

    async def prepare_successor(self, u, m, vid, decision_start, denied):
        """Prepare one same-workload successor without double-counting its demand."""
        predecessor_id = m["predecessor_video_id"]
        predecessor = self.videos.get(predecessor_id)
        if (
            predecessor is None
            or predecessor.uid != u.uid
            or u.active_video != predecessor_id
            or predecessor.state in TERMINAL
        ):
            return denied("invalid_predecessor")
        if u.prepared_video and self.videos[u.prepared_video].state not in TERMINAL:
            return denied("successor_exists")
        if m.get("application") != predecessor.app:
            return denied("successor_workload_changed")
        fmt = m.get("input", {})
        height, width = self.catalog["source_hw"]
        if (fmt.get("width"), fmt.get("height"), fmt.get("fps")) != (
            width,
            height,
            self.cfg.fps,
        ):
            return denied("unsupported_profiled_input")
        slo = float(m.get("slo_ms", predecessor.slo * 1000)) / 1000
        if not math.isfinite(slo) or slo <= 0 or abs(slo - predecessor.slo) > 1e-9:
            return denied("successor_workload_changed")
        start_unix_ns = integer(m["expected_start_unix_ns"])
        basis = predecessor.applied or predecessor.pending or predecessor.current
        if not basis or basis["candidate_id"] in self.quarantined:
            return denied("successor_configuration_unavailable")

        used = {
            x.port for x in self.videos.values() if x.state not in TERMINAL and x.port
        }
        port = next(
            (
                p
                for p in range(
                    self.cfg.first_udp_port,
                    self.cfg.first_udp_port + self.cfg.max_videos * 2,
                )
                if p not in used
            ),
            None,
        )
        if port is None:
            return denied("receiver_capacity")

        self.plan_version += 1
        self.config_version += 1
        cfg = dict(
            basis,
            command_id=str(uuid.uuid4()),
            control_version=1,
            stop_unix_ns=None,
            suspension_version=0,
            config_version=self.config_version,
            plan_version=self.plan_version,
            boundary_unix_ns=start_unix_ns,
            first_frame_id=0,
            first_tx_unix_ns=start_unix_ns,
        )
        v = Video(
            vid,
            u.uid,
            predecessor.app,
            secrets.randbits(52) or 1,
            time.monotonic(),
            state="PREPARED",
            port=port,
            slo=slo,
            can_drop=predecessor.can_drop,
            can_drop_frames=predecessor.can_drop_frames,
            predecessor_id=predecessor_id,
            expected_start=time.monotonic()
            + max(0.0, (start_unix_ns - time.time_ns()) / 1e9),
        )
        candidate = self.candidates[basis["candidate_id"]]
        v.configs[cfg["config_version"]] = candidate
        v.plans[cfg["plan_version"]] = cfg
        v.pending = cfg
        v.control_version = 1
        v.transmission_control = dict(
            control_version=1,
            command_id=cfg["command_id"],
            status="ACTIVE",
            suspension_version=0,
            effective_unix_ns=start_unix_ns,
            plan_version=cfg["plan_version"],
            stop_unix_ns=None,
        )
        receiver = (MockReceiver if self.cfg.backend == "mock" else Receiver)(
            vid, v.epoch, port, self.cfg, self.post
        )
        try:
            await asyncio.to_thread(receiver.start)
        except Exception as ex:
            await asyncio.to_thread(receiver.close)
            self.record.event("receiver_setup_failed", video_id=vid, reason=repr(ex))
            return denied("setup_failed")
        v.receiver = receiver
        if v.app == "tracking":
            # Each video owns a fresh tracker, so state never crosses the handoff.
            v.tracker = Tracker(
                self.cfg.fps, self.cfg.tracker_retention_s, self.cfg.backend == "mock"
            )
        self.videos[vid] = v
        u.prepared_video = vid
        self.aware(
            "session",
            ue_id=v.uid,
            video_id=v.vid,
            media_epoch=v.epoch,
            expectation=self.expectation(v, cfg, False),
            transmission_control=v.transmission_control,
        )
        decision_end = time.monotonic_ns()
        self.record.event(
            "admission_decision",
            ue_id=u.uid,
            video_id=vid,
            predecessor_video_id=predecessor_id,
            client_seq=m["client_seq"],
            application=v.app,
            input=m.get("input"),
            accepted=True,
            reason=None,
            decision_start_monotonic_ns=decision_start,
            decision_end_monotonic_ns=decision_end,
            decision_latency_ns=decision_end - decision_start,
        )
        self.record.event(
            "successor_prepared",
            ue_id=u.uid,
            video_id=vid,
            predecessor_video_id=predecessor_id,
            expected_start_unix_ns=start_unix_ns,
        )
        return self.video_response(u, v, m["client_seq"])

    # Advertise future phased boundaries; sent plans remain pending until media evidence.
    def plan(self, vs, proposal):
        self.plan_version += 1
        members = sorted(vs, key=lambda v: (v.admitted, v.uid))
        n = len(members)
        now = time.time_ns()
        period_ns = round(self.cfg.gop_frames / self.cfg.fps * 1e9)
        activation = math.ceil(
            (now + self.cfg.gop_lead_s * 1e9 - self.epoch_unix_ns) / period_ns
        )
        for i, v in enumerate(members):
            self.config_version += 1
            v.control_version += 1
            cid = proposal[v.uid]
            c = self.candidates[cid]
            offset = (
                i * (self.cfg.gop_frames // n)
                if self.cfg.keyframe_interleaving.enabled
                else 0
            )
            boundary = (
                self.epoch_unix_ns
                + activation * period_ns
                + round(offset / self.cfg.fps * 1e9)
            )
            basis = v.applied or v.pending
            first_frame = 0
            if basis:
                # Project the existing source timeline to the future activation boundary.
                first_frame = basis.get("first_frame_id", 0) + max(
                    0,
                    round(
                        (boundary - basis.get("first_tx_unix_ns", basis["boundary_unix_ns"]))
                        * basis["fps"]
                        / 1e9
                    ),
                )
            cfg = dict(
                command_id=str(uuid.uuid4()),
                control_version=v.control_version,
                stop_unix_ns=v.suspension_boundary,
                suspension_version=v.suspension_version,
                catalog_version=self.catalog["version"],
                config_version=self.config_version,
                plan_version=self.plan_version,
                activation_cycle=activation,
                boundary_unix_ns=boundary,
                epoch_unix_ns=self.epoch_unix_ns,
                offset_frames=offset,
                gop_frames=self.cfg.gop_frames,
                fps=self.cfg.fps,
                first_frame_id=first_frame,
                width=self.catalog["source_hw"][1],
                height=self.catalog["source_hw"][0],
                bitrate_bps=c["bitrate_bps"],
                model=c["model"],
                candidate_id=cid,
                input_hw=c["input_hw"],
                precision=c["precision"],
            )
            v.configs[self.config_version] = c
            v.plans[self.plan_version] = cfg
            v.pending = cfg
            v.transmission_control = dict(
                control_version=v.control_version,
                command_id=cfg["command_id"],
                status="ACTIVE",
                suspension_version=v.suspension_version,
                effective_unix_ns=boundary,
                plan_version=self.plan_version,
                stop_unix_ns=v.suspension_boundary,
            )
            self.send(
                v.uid,
                envelope(
                    "gop_config", self.cfg.run_id, ue_id=v.uid, video_id=v.vid, **cfg
                ),
            )
            self.aware(
                "session",
                ue_id=v.uid,
                video_id=v.vid,
                media_epoch=v.epoch,
                expectation=self.expectation(v, cfg, False),
                transmission_control=v.transmission_control,
            )
        self.last_members = tuple(v.vid for v in members)

    def expectation(self, v, cfg, applied):
        c = self.candidates[cfg["candidate_id"]]
        res = self.residence_for(v, c["id"]).value
        return dict(
            applied=applied,
            control_version=cfg.get("control_version", 0),
            config_version=cfg["config_version"],
            plan_version=cfg["plan_version"],
            fps=cfg["fps"],
            gop_frames=cfg["gop_frames"],
            epoch_unix_ns=cfg["epoch_unix_ns"],
            offset_frames=cfg["offset_frames"],
            activation_cycle=cfg["activation_cycle"],
            first_tx_unix_ns=cfg.get("first_tx_unix_ns", cfg["boundary_unix_ns"]),
            first_frame_id=cfg.get("first_frame_id", 0),
            network_budget_ns=round((v.slo - res) * 1e9),
            size_profile={"I": c["frame_bytes_I"], "P": c["frame_bytes_P"]},
        )

    # Reserve network time using full MEC residence, separate from processing medians.
    def residence_for(self, v, cid):
        c = self.candidates[cid]
        key = (
            v.uid,
            c["application"],
            c["model"],
            tuple(c["input_hw"]),
            c["precision"],
        )
        if key not in self.residence:
            # Collection has no heuristic residence calibration requirement;
            # measured processing is a labeled lower-bound until live samples replace it.
            initial = self.candidates[cid].get("residence_s")
            if initial is None:
                initial = self.candidates[cid]["processing_s"]
            self.residence[key] = History(initial, "mean")
        return self.residence[key]

    def end_video(self, u, m):
        v = self.videos.get(m["video_id"])
        if not v or v.uid != u.uid:
            raise ValueError("video identity mismatch")
        # A later command cannot reopen a lifecycle the drain loop already finalized.
        if v.state not in TERMINAL:
            v.final_id = integer(m["final_frame_id"])
            v.state = "DRAINING"
            v.input_close_at = time.monotonic() + self.cfg.final_arrival_s
            if self.collection_mode:
                # The sender can finish while completely received access units
                # remain queued in GStreamer. Bound that decoder drain without
                # converting valid buffered frames into immediate failures.
                v.drain_until = time.monotonic() + self.cfg.drain_s
            # Stop future radio predictions immediately while accepting late UDP packets.
            self.close_demand(v)
            if u.prepared_video:
                successor = self.videos.get(u.prepared_video)
                if successor and successor.predecessor_id == v.vid:
                    self.promote_successor(successor, "video_end")
        return envelope(
            "video_end_ack",
            self.cfg.run_id,
            ue_id=u.uid,
            video_id=v.vid,
            client_seq=m["client_seq"],
            status=v.state,
        )

    def close_demand(self, v):
        """Stop future predictions once while preserving the late-packet ledger."""
        if v.demand_closed:
            return
        v.demand_closed = True
        v.control_version += 1
        stop = time.time_ns()
        v.transmission_control = dict(
            control_version=v.control_version,
            command_id=str(uuid.uuid4()),
            status="ACTIVE",
            suspension_version=v.suspension_version,
            effective_unix_ns=stop,
            plan_version=(v.applied or v.pending or v.current or {}).get(
                "plan_version", 0
            ),
            stop_unix_ns=stop,
        )
        self.aware(
            "session",
            ue_id=v.uid,
            video_id=v.vid,
            media_epoch=v.epoch,
            transmission_control=v.transmission_control,
        )

    def promote_successor(self, v, trigger):
        """Make a prepared successor current regardless of cross-transport ordering."""
        if v.state != "PREPARED":
            return
        u = self.ues[v.uid]
        predecessor = self.videos.get(v.predecessor_id)
        if predecessor and predecessor.state not in TERMINAL:
            self.close_demand(predecessor)
            if predecessor.state not in ("DRAINING", "HANDOFF_PENDING_END"):
                predecessor.state = "HANDOFF_PENDING_END"
        v.state = "AWAITING_DELIVERY"
        # First-media expiry begins at promotion, not at early receiver setup.
        v.admitted = time.monotonic()
        u.active_video = v.vid
        if u.prepared_video == v.vid:
            u.prepared_video = None
        self.record.event(
            "successor_promoted",
            ue_id=u.uid,
            video_id=v.vid,
            predecessor_video_id=v.predecessor_id,
            trigger=trigger,
        )

    # Media may arrive out of order, so only newer evidence can advance session state.
    def _observe_plan(self, v, d, plan):
        key = (d.plan_version, d.config_version)
        applied_key = (
            (v.applied["plan_version"], v.applied["config_version"])
            if v.applied
            else None
        )
        pending_key = (
            (v.pending["plan_version"], v.pending["config_version"])
            if v.pending
            else None
        )

        # A delayed frame retains its own plan without rolling back the session.
        if applied_key and key < applied_key:
            return v.applied

        if pending_key and key < pending_key:
            boundary_fid = v.pending.get("first_frame_id", 0)
            boundary_ns = v.pending["boundary_unix_ns"]
            # Ordered old-plan evidence beyond both boundaries means the switch was missed.
            if (
                d.frame_id >= boundary_fid
                and d.flags & 8
                and d.tx_unix_ns >= boundary_ns
            ):
                missed = v.pending
                v.pending = None
                self.record.event(
                    "configuration_unapplied",
                    video_id=v.vid,
                    frame_id=d.frame_id,
                    config_version=missed["config_version"],
                    plan_version=missed["plan_version"],
                )

        if applied_key and key == applied_key:
            return v.applied

        observed = dict(
            plan, first_frame_id=d.frame_id, first_tx_unix_ns=d.tx_unix_ns
        )
        v.applied = observed
        v.current = observed
        self.record.event(
            "configuration_observed",
            video_id=v.vid,
            frame_id=d.frame_id,
            config_version=d.config_version,
            plan_version=d.plan_version,
        )
        return observed

    # Forward actual encoded size before decode, retaining each frame's advertised model.
    def receive_descriptor(self, d, first_ns):
        v = self.videos.get(d.video_id)
        if (
            not v
            or v.epoch != d.media_epoch
            or v.state in TERMINAL
            or v.resolved.has(d.frame_id)
        ):
            return
        if v.state == "PREPARED":
            # UDP may beat the predecessor's WebSocket video_end message.
            self.promote_successor(v, "first_descriptor")
        key = (v.vid, d.frame_id)
        if key in self.frames:
            if self.frames[key].descriptor != d:
                self.fail_frame(*key, "conflicting_descriptor")
            return
        c = v.configs.get(d.config_version)
        plan = v.plans.get(d.plan_version)
        if c is None or plan is None or plan["config_version"] != d.config_version:
            v.radio_frames[d.frame_id] = dict(
                frame_id=d.frame_id,
                tx_unix_ns=d.tx_unix_ns or time.time_ns(),
                network_deadline_unix_ns=None,
                encoded_bytes=d.encoded_bytes,
                config_version=d.config_version,
                unique_bytes=0,
                progress_seq=0,
                complete=False,
            )
            self.record.event(
                "invalid_frame_version", video_id=v.vid, frame_id=d.frame_id
            )
            self.resolve_missing(v, d.frame_id, "invalid_configuration_version")
            self.aware(
                "descriptor",
                0,
                ue_id=v.uid,
                video_id=v.vid,
                media_epoch=v.epoch,
                frame_id=d.frame_id,
                encoded_bytes=d.encoded_bytes,
                tx_unix_ns=d.tx_unix_ns or time.time_ns(),
                network_deadline_unix_ns=None,
                config_version=d.config_version,
            )
            self.send(
                v.uid,
                envelope(
                    "video_status",
                    self.cfg.run_id,
                    video_id=v.vid,
                    status=v.state,
                    reason="configuration_reconciliation_required",
                ),
            )
            return
        self.resolve_suspension_gap(v, d.frame_id, plan)
        f = Frame(v.vid, d.frame_id, d, first_ns, c)
        f.times["first_packet_monotonic_ns"] = first_ns
        self.frames[key] = f
        if v.state == "AWAITING_DELIVERY":
            v.state = "ACTIVE"
        v.received += 1
        applied_plan = self._observe_plan(v, d, plan)
        u = self.ues[v.uid]
        res = self.residence_for(v, c["id"]).value
        deadline = (
            d.tx_unix_ns + round((v.slo - res) * 1e9)
            if d.flags & 8
            and u.clock_uncertainty_ns is not None
            and u.clock_uncertainty_ns <= self.cfg.max_clock_uncertainty_ns
            else None
        )
        msg = dict(
            ue_id=v.uid,
            video_id=v.vid,
            media_epoch=v.epoch,
            frame_id=d.frame_id,
            encoded_bytes=d.encoded_bytes,
            tx_unix_ns=d.tx_unix_ns
            if d.flags & 8
            else first_ns + time.time_ns() - time.monotonic_ns(),
            network_deadline_unix_ns=deadline,
            config_version=d.config_version,
            plan_version=d.plan_version,
            frame_type=d.frame_type,
            nonpositive_network_budget=v.slo - res <= 0,
            clock_valid=deadline is not None,
            # Report current session state even when this frame used an older plan.
            applied_expectation=dict(
                self.expectation(v, applied_plan, True),
                first_frame_id=applied_plan.get("first_frame_id", d.frame_id),
                first_tx_unix_ns=applied_plan.get(
                    "first_tx_unix_ns", d.tx_unix_ns
                ),
            ),
        )
        v.radio_frames[d.frame_id] = dict(
            msg, unique_bytes=0, progress_seq=0, complete=False
        )
        self.aware("descriptor", 0, **msg)
        self.record.event(
            "descriptor",
            video_id=v.vid,
            frame_id=d.frame_id,
            encoded_bytes=d.encoded_bytes,
            network_deadline_unix_ns=deadline,
            nonpositive_network_budget=v.slo - res <= 0,
        )

    # Reception completion closes UL demand independently of the inference outcome.
    def progress_event(self, d, unique, complete, received_ns):
        v = self.videos.get(d.video_id)
        f = self.frames.get((d.video_id, d.frame_id))
        if not v:
            return
        radio = v.radio_frames.get(d.frame_id)
        if not radio:
            return
        radio["progress_seq"] += 1
        radio["unique_bytes"] = max(radio["unique_bytes"], unique)
        radio["complete"] = complete
        self.progress[(v.vid, d.frame_id)] = dict(
            ue_id=v.uid,
            video_id=v.vid,
            media_epoch=v.epoch,
            frame_id=d.frame_id,
            progress_seq=radio["progress_seq"],
            unique_bytes=unique,
            complete=complete,
            measurement_unix_ns=received_ns + time.time_ns() - time.monotonic_ns(),
        )
        if complete:
            if f:
                f.complete_ns = received_ns
                f.times["complete_reception_monotonic_ns"] = received_ns
                u = self.ues[v.uid]
                if u.network:
                    report = self.last_report or {}
                    r = report.get("ues", {}).get(v.uid, {})
                    valid = bool(
                        self.cfg.allow_scheduled_throughput_estimate
                        and r.get("throughput_valid")
                        and time.time_ns() - report.get("window_end_unix_ns", 0)
                        <= 300_000_000
                    )
                    u.network.observe(
                        (v.vid, d.frame_id),
                        d.encoded_bytes,
                        r.get("throughput_bytes_s") or 0,
                        f.config["bitrate_bps"],
                        time.monotonic(),
                        valid,
                    )
                    u.network.propagate(received_ns / 1e9)
                    f.network_s = f.config["bitrate_bps"] / 1e6 * u.network.mu
                else:
                    f.network_s = 0.0
                f.deadline_ns = f.complete_ns + round((v.slo - f.network_s) * 1e9)
            self.flush_progress((v.vid, d.frame_id))
            del v.radio_frames[d.frame_id]

    def flush_progress(self, key):
        msg = self.progress.pop(key, None)
        if msg:
            self.aware("progress", 0 if msg["complete"] else 1, **msg)
            self.last_progress[key] = time.monotonic()

    # Assign priority once, then enforce the per-video pending-input limits.
    def decoded(self, d, rgb, decoded_ns):
        f = self.frames.get((d.video_id, d.frame_id))
        v = self.videos.get(d.video_id)
        if not f or not v or v.resolved.has(d.frame_id):
            return
        if f.complete_ns is None:
            self.fail_frame(v.vid, d.frame_id, "incomplete_encoded_access_unit")
            return
        if v.input_closed and not self.collection_mode:
            self.fail_frame(v.vid, d.frame_id, "input_closed")
            return
        if f.config["id"] in self.quarantined:
            self.fail_frame(v.vid, d.frame_id, "execution_candidate_quarantined")
            return
        if (
            v.suspension_boundary is not None
            and d.tx_unix_ns >= v.suspension_boundary
            and v.plans[d.plan_version].get("control_version", 0) <= v.suspension_version
        ):
            self.fail_frame(v.vid, d.frame_id, "session_suspended")
            return
        f.state = "PRIORITY_PENDING"
        f.decoded_ns = decoded_ns
        f.rgb = rgb
        f.times["decoded_monotonic_ns"] = decoded_ns
        f.times["decode_s"] = (decoded_ns - f.complete_ns) / 1e9
        # Collection uses one configured stream; deadlines remain evidence only.
        decision_start = time.monotonic_ns()
        waiting = max(0, (time.monotonic_ns() - f.complete_ns) / 1e9)
        f.priority = (
            self.cfg.priority_streams[0]
            if self.collection_mode
            else self.priority.propose(
                v.slo,
                f.network_s,
                waiting,
                self.queue_history.value,
                self.processing(f.config["id"]),
            )
        )
        decision_end = time.monotonic_ns()
        f.times.update(
            priority_decision_start_monotonic_ns=decision_start,
            priority_decision_end_monotonic_ns=decision_end,
            priority_decision_latency_ns=decision_end - decision_start,
        )
        self.record.event(
            "priority_decision",
            decision_type="frame_priority",
            policy_version=(
                "collection-fixed-v1"
                if self.collection_mode
                else self.priority.version
            ),
            video_id=v.vid,
            frame_id=f.fid,
            proposed=f.priority,
            validated=f.priority is not None,
            executed=f.priority,
            fallback_reason="nonpositive_slack" if f.priority is None else None,
            decision_start_monotonic_ns=decision_start,
            decision_end_monotonic_ns=decision_end,
            decision_latency_ns=decision_end - decision_start,
            network_s=f.network_s,
            waiting_s=waiting,
        )
        if f.priority is None:
            self.fail_frame(v.vid, d.frame_id, "nonpositive_slack")
            return
        pending = [
            x
            for x in self.frames.values()
            if x.vid == v.vid and x.state in ("PRIORITY_PENDING", "QUEUED")
        ]
        over_limit = (
            len(pending) > self.cfg.pending_frames
            or sum(x.rgb.nbytes for x in pending if x.rgb is not None)
            > self.cfg.pending_bytes_per_video
        )
        if over_limit and self.collection_mode:
            v.collection_error = "collection_buffer_limit"
            self.record.event(
                "collection_run_invalid",
                video_id=v.vid,
                reason=v.collection_error,
                pending_frames=len(pending),
                pending_bytes=sum(
                    x.rgb.nbytes for x in pending if x.rgb is not None
                ),
            )
            self.fail_frame(v.vid, d.frame_id, v.collection_error)
            return
        while over_limit:
            oldest = min(pending, key=lambda x: x.fid)
            pending.remove(oldest)
            self.fail_frame(v.vid, oldest.fid, "buffer_overflow")
            over_limit = (
                len(pending) > self.cfg.pending_frames
                or sum(x.rgb.nbytes for x in pending if x.rgb is not None)
                > self.cfg.pending_bytes_per_video
            )
        if v.resolved.has(d.frame_id):
            return
        f.queue_ns = time.monotonic_ns()
        f.times["queue_enter_monotonic_ns"] = f.queue_ns
        f.state = "QUEUED"
        self.pending[(v.app, f.priority)].append((v.vid, f.fid))

    def fail_frame(self, vid, fid, reason):
        f = self.frames.get((vid, fid))
        v = self.videos.get(vid)
        if not v or v.resolved.has(fid):
            return
        if f is None:
            return
        self.resolve(
            f,
            "failed"
            if self.collection_mode
            or reason
            in (
                "execution_failed",
                "execution_candidate_quarantined",
                "transport_loss",
                "invalid_metadata",
                "collection_event_overflow",
                "collection_buffer_limit",
                "collection_drain_watchdog",
            )
            else "dropped",
            reason,
        )

    # The first resumed descriptor closes the intentional source-frame gap.
    def resolve_suspension_gap(self, v, fid, plan):
        previous = v.last_descriptor_id
        v.last_descriptor_id = max(previous, fid)
        version = plan.get("control_version", 0)
        if (
            not v.suspension_boundary
            or version <= v.suspension_version
            or version <= v.suspension_gap_version
        ):
            return
        for skipped in range(previous + 1, fid):
            self.resolve_missing(v, skipped, "session_suspended", "dropped")
        v.suspension_gap_version = version

    # Frames without descriptors cannot supply trustworthy GoP or model metadata.
    def resolve_missing(self, v, fid, reason, outcome="failed"):
        if v.resolved.has(fid):
            return
        v.resolved.add(fid)
        v.counters[outcome] += 1
        m = envelope(
            "frame_result",
            self.cfg.run_id,
            ue_id=v.uid,
            video_id=v.vid,
            request_id=f"{v.vid}/{fid}",
            frame_id=fid,
            result_id=result_id(v.vid, fid, outcome),
            gop_id=None,
            config_version=None,
            plan_version=None,
            application=v.app,
            task_id=v.vid,
            model=None,
            candidate_id=None,
            bitrate_bps=None,
            priority=None,
            outcome=outcome,
            reason=reason,
            stage_times={},
            payload=None,
            loss_detected_at_unix_ns=time.time_ns(),
            metadata_available=False,
        )
        v.deliveries += 1
        self.task(self.deliver(v, m, None, time.monotonic_ns(), None))

    # Fence the outcome before asynchronous serialization and durable replay storage.
    def resolve(self, f, outcome, reason=None, payload=None):
        v = self.videos[f.vid]
        if v.resolved.has(f.fid):
            return
        v.resolved.add(f.fid)
        v.counters[outcome] += 1
        rid = result_id(v.vid, f.fid, outcome)
        now = time.monotonic_ns()
        f.times["result_ready_monotonic_ns"] = now
        message = envelope(
            "frame_result",
            self.cfg.run_id,
            ue_id=v.uid,
            video_id=v.vid,
            request_id=f"{v.vid}/{f.fid}",
            frame_id=f.fid,
            result_id=rid,
            gop_id=f.descriptor.gop_id,
            config_version=f.descriptor.config_version,
            plan_version=f.descriptor.plan_version,
            application=v.app,
            task_id=v.vid,
            model=f.config["model"],
            candidate_id=f.config["id"],
            bitrate_bps=f.config["bitrate_bps"],
            priority=f.priority,
            outcome=outcome,
            reason=reason,
            stage_times=f.times,
            payload=payload,
        )
        f.rgb = None
        self.frames.pop((f.vid, f.fid), None)
        v.tracking_ready.pop(f.fid, None)
        v.deliveries += 1
        self.task(self.deliver(v, message, f.complete_ns, now, f.config["id"]))

    async def deliver(self, v, message, complete_ns, ready_ns, cid):
        try:
            # Freeze scenario identity before durable replay or asynchronous delivery.
            self.contextual(message)
            begin = time.monotonic_ns()
            await asyncio.to_thread(dumps, message)
            prepared = time.monotonic_ns()
            message["stage_times"].update(
                result_prepare_start_monotonic_ns=begin,
                result_prepare_end_monotonic_ns=prepared,
                result_prepare_s=(prepared - begin) / 1e9,
            )
            self.record.put(
                "jobs.jsonl",
                {k: value for k, value in message.items() if k != "payload"},
            )
            if message["outcome"] == "completed" and complete_ns:
                residence = (time.monotonic_ns() - complete_ns) / 1e9
                self.residence_for(v, cid).add(residence)
                self.record.event(
                    "residence",
                    ue_id=v.uid,
                    video_id=v.vid,
                    frame_id=message["frame_id"],
                    candidate_id=cid,
                    residence_s=residence,
                )
            await asyncio.to_thread(
                self.replay.save,
                v.uid,
                v.vid,
                message["frame_id"],
                message["result_id"],
                message,
            )
            self.send(v.uid, message)
        except Exception as ex:
            if self.collection_mode:
                v.collection_error = "result_delivery_failed"
                self.record.event(
                    "collection_run_invalid",
                    video_id=v.vid,
                    frame_id=message["frame_id"],
                    reason=v.collection_error,
                    error=repr(ex),
                )
            raise
        finally:
            v.deliveries -= 1

    # Drain events, dispatch free streams, and advance lifecycle timers on one loop.
    async def tick(self):
        while True:
            now = time.monotonic()
            ns = time.monotonic_ns()
            for _ in range(1024):
                try:
                    kind, data = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "descriptor":
                        self.receive_descriptor(
                            data["descriptor"], data["first_packet_ns"]
                        )
                    elif kind == "progress":
                        self.progress_event(
                            data["descriptor"],
                            data["unique_bytes"],
                            data["complete"],
                            data["received_ns"],
                        )
                    elif kind == "decoded":
                        self.decoded(
                            data["descriptor"], data["rgb"], data["decoded_ns"]
                        )
                    elif kind == "frame_failure":
                        d = data["descriptor"]
                        self.fail_frame(d.video_id, d.frame_id, data["reason"])
                    else:
                        self.record.event(kind, **data)
                except Exception as ex:
                    self.record.event(
                        "receiver_event_error", kind=kind, reason=repr(ex)
                    )
            for key in list(self.progress):
                if now - self.last_progress.get(key, 0) >= 0.005:
                    self.flush_progress(key)
            for f in list(self.frames.values()):
                if (
                    not self.collection_mode
                    and f.state == "RECEIVING"
                    and f.complete_ns is not None
                    and ns >= f.deadline_ns
                ):
                    self.fail_frame(f.vid, f.fid, "decode_deadline_expired")
            for m in self.workers.poll():
                if m["type"] == "startup_failed":
                    self.worker_failures[m["application"]] = m["error"]
                if m["type"] in ("completed", "failed"):
                    vid, fid = m["job_id"]
                    context = self.inflight.pop((vid, fid), None)
                    if context and m["type"] == "completed":
                        self.histories[context["candidate_id"]][0].add(
                            m["times"]["processing_s"]
                        )
                        self.queue_history.add(
                            (m["worker_start_ns"] - context["queue_ns"]) / 1e9
                        )
                    f = self.frames.get((vid, fid))
                    if not f:
                        self.record.event(
                            "discarded_gpu_completion", job_id=m["job_id"]
                        )
                        continue
                    if m["type"] == "failed":
                        f.times.update(
                            worker_start_monotonic_ns=m["worker_start_ns"],
                            worker_end_monotonic_ns=m["worker_finish_ns"],
                            worker_elapsed_ns=m["worker_finish_ns"]
                            - m["worker_start_ns"],
                        )
                        self.quarantine_candidate(f.config["id"], m.get("error"))
                        self.fail_frame(vid, fid, "execution_failed")
                        continue
                    v = self.videos[vid]
                    f.times.update(m["times"])
                    f.times.update(
                        worker_start_monotonic_ns=m["worker_start_ns"],
                        worker_end_monotonic_ns=m["worker_finish_ns"],
                        worker_elapsed_ns=m["worker_finish_ns"]
                        - m["worker_start_ns"],
                    )
                    wait = (m["worker_start_ns"] - f.queue_ns) / 1e9
                    f.times["queue_wait_s"] = wait
                    f.payload = m["payload"]
                    f.rgb = None
                    if v.app == "tracking":
                        f.state = "REORDER_WAIT"
                        f.detection_ready_ns = ns
                        v.tracking_ready[fid] = f
                        while (
                            not self.collection_mode
                            and len(v.tracking_ready) > self.cfg.tracker_buffer
                        ):
                            oldest = min(v.tracking_ready)
                            self.fail_frame(vid, oldest, "tracking_buffer_overflow")
                    else:
                        self.resolve(f, "completed", payload=f.payload)
            for (app, p), items in self.pending.items():
                if not self.workers.available(app, p):
                    continue
                while items:
                    key = items.popleft()
                    f = self.frames.get(key)
                    if not f or f.state != "QUEUED":
                        continue
                    if f.config["id"] in self.quarantined:
                        self.fail_frame(*key, "execution_candidate_quarantined")
                        continue
                    if not self.collection_mode and ns >= f.deadline_ns:
                        self.fail_frame(*key, "deadline_expired")
                        continue
                    job = dict(
                        job_id=list(key), priority=p, configuration=f.config, rgb=f.rgb
                    )
                    if self.workers.submit(app, p, job):
                        self.inflight[key] = dict(
                            candidate_id=f.config["id"],
                            queue_ns=f.queue_ns,
                            submitted_ns=ns,
                            application=app,
                            priority=p,
                        )
                        f.state = "PROCESSING"
                    else:
                        items.appendleft(key)
                    break
            for v in list(self.videos.values()):
                if v.finalized:
                    continue
                if v.receiver:
                    v.receiver.tick()
                if v.app == "tracking":
                    self.tracker_tick(v, ns)
                u = self.ues[v.uid]
                if (
                    not u.connected
                    and u.disconnect_at is not None
                    and now - u.disconnect_at >= self.cfg.disconnect_s
                ):
                    self.close_input(v, "control_disconnect")
                    v.state = "EXPIRED"
                if (
                    v.state == "AWAITING_DELIVERY"
                    and now - v.admitted >= self.cfg.first_media_s
                ):
                    self.close_input(v, "first_media_timeout")
                    v.state = "EXPIRED"
                    if u.active_video == v.vid:
                        u.active_video = None
                if (
                    v.state == "PREPARED"
                    and v.expected_start is not None
                    and now >= v.expected_start + self.cfg.first_media_s
                ):
                    self.close_input(v, "prepared_successor_timeout")
                    v.state = "EXPIRED"
                    if u.prepared_video == v.vid:
                        u.prepared_video = None
                    self.send(
                        v.uid,
                        envelope(
                            "video_status",
                            self.cfg.run_id,
                            video_id=v.vid,
                            status=v.state,
                            reason="prepared_successor_timeout",
                        ),
                    )
                if (
                    v.input_close_at is not None
                    and now >= v.input_close_at
                    and not v.input_closed
                ):
                    receiving = any(
                        f.vid == v.vid and f.state == "RECEIVING"
                        for f in self.frames.values()
                    )
                    if not (
                        self.collection_mode
                        and receiving
                        and v.drain_until is not None
                        and now < v.drain_until
                    ):
                        self.close_input(v, "video_end")
                if v.input_closed:
                    if v.final_id is not None:
                        for fid in range(
                            v.gap_scan, min(v.gap_scan + 256, v.final_id + 1)
                        ):
                            if (v.vid, fid) not in self.frames and not v.resolved.has(
                                fid
                            ):
                                self.resolve_missing(
                                    v, fid, "declared_missing_after_final_frame"
                                )
                        v.gap_scan = min(v.gap_scan + 256, v.final_id + 1)
                    outstanding = [f for f in self.frames.values() if f.vid == v.vid]
                    gaps_done = v.final_id is None or v.gap_scan > v.final_id
                    drained = (
                        gaps_done
                        and not outstanding
                        and not v.tracker_busy
                        and v.deliveries == 0
                    )
                    watchdog = now >= v.drain_until
                    if drained or watchdog:
                        if watchdog and not drained:
                            reason = (
                                "collection_drain_watchdog"
                                if self.collection_mode
                                else "drain_timeout"
                            )
                            if self.collection_mode:
                                if v.collection_error != reason:
                                    v.collection_error = reason
                                    self.record.event(
                                        "collection_run_invalid",
                                        video_id=v.vid,
                                        reason=reason,
                                        outstanding_frames=len(outstanding),
                                        pending_deliveries=v.deliveries,
                                        tracker_busy=v.tracker_busy,
                                    )
                            for f in outstanding:
                                self.fail_frame(v.vid, f.fid, reason)
                            # Result persistence/delivery may finish after frames resolve.
                            if v.deliveries or v.tracker_busy:
                                continue
                        if v.state not in TERMINAL:
                            v.state = (
                                "FAILED"
                                if self.collection_mode and v.collection_error
                                else "COMPLETED"
                            )
                        if u.active_video == v.vid:
                            u.active_video = None
                        if u.prepared_video == v.vid:
                            u.prepared_video = None
                        v.finalized = True
                        self.send(
                            v.uid,
                            envelope(
                                "video_status",
                                self.cfg.run_id,
                                video_id=v.vid,
                                status=v.state,
                                counters=v.counters,
                                reason=v.collection_error,
                            ),
                        )
            period_ns = round(self.cfg.gop_frames / self.cfg.fps * 1e9)
            cycle = (time.time_ns() - self.epoch_unix_ns) // period_ns
            if (
                cycle != self.last_cycle
                and not self.closed
                and not self.collection_mode
            ):
                self.last_cycle = cycle
                self.coordinate()
            if now - self.last_snapshot >= self.cfg.snapshot_s:
                self.last_snapshot = now
                self.snapshot()
            await asyncio.sleep(0.002)

    # Use the earliest buffered tracking cutoff to bound waits on known predecessors.
    def tracker_tick(self, v, ns):
        if v.tracker_busy:
            return
        fs = sorted(
            [f for f in self.frames.values() if f.vid == v.vid], key=lambda f: f.fid
        )
        ready = sorted(v.tracking_ready)
        if not ready:
            return
        if self.collection_mode:
            # Wait for known predecessors, then update the per-video tracker in order.
            target = ready[0]
            if any(f.fid < target for f in fs):
                return
            f = self.frames.get((v.vid, target))
            if not f:
                return
            v.tracker_busy = True
            f.state = "TRACKING"
            v.tracking_ready.pop(f.fid, None)
            self.task(self.track(v, f))
            return
        cutoff = min(
            self.frames[(v.vid, fid)].deadline_ns
            - round(
                (
                    self.histories[self.frames[(v.vid, fid)].config["id"]][1].value
                    + self.cfg.tracker_margin_s
                )
                * 1e9
            )
            for fid in ready
        )
        target = ready[0]
        blockers = [f for f in fs if f.fid < target]
        if blockers and (ns >= cutoff or len(ready) > self.cfg.tracker_buffer):
            for f in blockers:
                self.fail_frame(v.vid, f.fid, "tracking_cutoff")
            blockers = []
        if blockers:
            return
        f = self.frames.get((v.vid, target))
        if not f:
            return
        if ns >= f.deadline_ns:
            self.fail_frame(v.vid, target, "deadline_expired")
            return
        v.tracker_busy = True
        f.state = "TRACKING"
        v.tracking_ready.pop(f.fid, None)
        self.task(self.track(v, f))

    # Serialize updates per tracker while letting other videos and GPU streams proceed.
    async def track(self, v, f):
        try:
            start = time.monotonic_ns()
            f.times["reorder_wait_s"] = (start - f.detection_ready_ns) / 1e9
            payload = await asyncio.to_thread(v.tracker.update, f.fid, f.payload)
            duration = (time.monotonic_ns() - start) / 1e9
            self.histories[f.config["id"]][1].add(duration)
            f.times["tracker_s"] = duration
            self.resolve(f, "completed", payload=payload)
        except Exception as ex:
            self.record.event("tracker_error", reason=repr(ex))
            self.fail_frame(v.vid, f.fid, "execution_failed")
        finally:
            v.tracker_busy = False

    # Closing media starts drain; shared application workers remain resident.
    def close_input(self, v, reason):
        if v.input_closed:
            return
        v.input_closed = True
        if v.drain_until is None:
            v.drain_until = time.monotonic() + self.cfg.drain_s
        if v.receiver:
            self.task(asyncio.to_thread(v.receiver.close))
        self.close_demand(v)
        if reason == "video_end" and self.collection_mode:
            # Aetheredge: stopping GStreamer cannot produce another decoded
            # sample. Resolve received tail frames explicitly instead of leaving
            # them in RECEIVING until the collection watchdog invalidates a run.
            for f in list(self.frames.values()):
                if f.vid == v.vid and f.state == "RECEIVING":
                    self.fail_frame(
                        v.vid,
                        f.fid,
                        "transport_loss"
                        if f.complete_ns is None
                        else "decode_incomplete_after_video_end",
                    )
        elif reason != "video_end":
            for f in list(self.frames.values()):
                if f.vid == v.vid and f.state != "PROCESSING":
                    self.fail_frame(v.vid, f.fid, reason)
        self.aware(
            "session",
            ue_id=v.uid,
            video_id=v.vid,
            media_epoch=v.epoch,
            input_closed=True,
        )
        self.record.event("input_closed", video_id=v.vid, reason=reason)

    def suspend_video(self, v):
        # Follow the latest advertised phase, including a pending transition.
        # The command identifies that plan so older queued plans cannot undo the stop.
        schedule = v.pending or v.applied or v.current
        earliest = time.time_ns() + round(self.cfg.gop_lead_s * 1e9)
        period = round(schedule["gop_frames"] / schedule["fps"] * 1e9)
        phase = schedule["epoch_unix_ns"] + round(
            schedule["offset_frames"] / schedule["fps"] * 1e9
        )
        earliest = max(earliest, schedule["boundary_unix_ns"])
        cycle = max(0, (earliest - phase + period - 1) // period)
        boundary = phase + cycle * period
        v.control_version += 1
        v.suspension_version = v.control_version
        v.suspension_boundary = boundary
        v.state = "SUSPENDED"
        control = dict(
            command_id=str(uuid.uuid4()),
            control_version=v.control_version,
            status="SUSPENDED",
            effective_unix_ns=boundary,
            activation_cycle=cycle,
            plan_version=schedule["plan_version"],
            epoch_unix_ns=schedule["epoch_unix_ns"],
            offset_frames=schedule["offset_frames"],
            fps=schedule["fps"],
            gop_frames=schedule["gop_frames"],
            stop_unix_ns=boundary,
        )
        v.transmission_control = control
        self.send(
            v.uid,
            envelope(
                "video_status",
                self.cfg.run_id,
                ue_id=v.uid,
                video_id=v.vid,
                reason="joint_budget_search_failed",
                **control,
            ),
        )
        self.aware(
            "session",
            ue_id=v.uid,
            video_id=v.vid,
            media_epoch=v.epoch,
            transmission_control=control,
        )
        self.record.event("suspension_scheduled", video_id=v.vid, **control)

    # At each master cycle, remove overload newest-first and reconsider suspended work.
    def coordinate(self):
        if self.collection_mode:
            return
        active = [
            v
            for v in self.videos.values()
            if v.state in ("ACTIVE", "AWAITING_DELIVERY")
        ]
        if any(not self.usable_radio(v.uid) for v in active):
            self.record.event("gop_frozen", reason="telemetry_unusable")
            return
        candidates = active[:]
        proposal = None
        while candidates:
            proposal = self.choose(candidates)
            if proposal is not None:
                break
            newest = max(candidates, key=lambda v: v.admitted)
            candidates.remove(newest)
            self.suspend_video(newest)
        # Recovery must also run when suspension left no active videos (M47/D57).
        for v in sorted(
            [v for v in self.videos.values() if v.state == "SUSPENDED"],
            key=lambda v: v.admitted,
        ):
            if not self.usable_radio(v.uid, admission=True):
                continue

            trial = candidates + [v]
            trial_proposal = self.choose(trial)
            if trial_proposal is None:
                continue

            candidates = trial
            proposal = trial_proposal
            v.state = "ACTIVE"

        if candidates and proposal is not None:
            self.plan(candidates, proposal)

    def snapshot(self):
        self.record.snapshot(
            dict(
                run_id=self.cfg.run_id,
                settings=self.cfg.model_dump(),
                catalog_version=self.catalog["version"],
                ues={
                    uid: dict(
                        connection_id=u.connection_id,
                        connected=u.connected,
                        active_video=u.active_video,
                        prepared_video=u.prepared_video,
                        reported_ip=u.ip,
                        observed_peer_ip=u.peer_ip,
                        clock_mapping=u.clock_mapping,
                        clock_uncertainty_ns=u.clock_uncertainty_ns,
                        network_estimator=dict(
                            mu_s=u.network.mu,
                            P_s2=u.network.P,
                            last_observation_monotonic_s=u.network.last_observation,
                        )
                        if u.network
                        else None,
                    )
                    for uid, u in self.ues.items()
                },
                videos={
                    vid: dict(
                        ue_id=v.uid,
                        application=v.app,
                        state=v.state,
                        media_epoch=v.epoch,
                        current=v.current,
                        pending=v.pending,
                        applied=v.applied,
                        transmission_control=v.transmission_control,
                        counters=v.counters,
                    )
                    for vid, v in self.videos.items()
                },
                unresolved=[
                    dict(
                        video_id=f.vid,
                        frame_id=f.fid,
                        state=f.state,
                        priority=f.priority,
                    )
                    for f in self.frames.values()
                ],
                inflight=[
                    dict(video_id=k[0], frame_id=k[1], **value)
                    for k, value in self.inflight.items()
                ],
                queues={
                    f"{a}/{p}": len(items) for (a, p), items in self.pending.items()
                },
                timing_histories={
                    cid: dict(
                        processing_s=list(h[0].values), tracker_s=list(h[1].values)
                    )
                    for cid, h in self.histories.items()
                },
                global_queue_history_s=list(self.queue_history.values),
                workers=self.workers.ready,
                quarantined=sorted(self.quarantined),
                media_bridge_errors=self.media_bridge_errors,
            )
        )

    # RPC retries run separately from reception; overflow requests a state resync.
    async def send_awareness(self):
        while True:
            try:
                if self.sync_needed:
                    snapshot_revision = self.awareness_seq
                    # A concurrent queue overflow sets this back to true while RPC waits.
                    self.sync_needed = False
                    message = envelope(
                        "sync",
                        self.cfg.run_id,
                        awareness_generation=self.awareness_generation,
                        # The snapshot includes every state change created through this revision.
                        awareness_revision=snapshot_revision,
                        associations=[
                            dict(
                                ue_id=u.uid,
                                connection_id=u.connection_id,
                                ip=u.ip,
                                connection_generation=u.generation,
                                association_revision=u.association_revision,
                                ue_alias=u.alias,
                            )
                            for u in self.ues.values()
                        ],
                        sessions=[
                            dict(
                                ue_id=v.uid,
                                video_id=v.vid,
                                media_epoch=v.epoch,
                                input_closed=v.input_closed,
                                transmission_control=v.transmission_control,
                                expectation=self.expectation(v, v.applied, True)
                                if v.applied
                                else None,
                                pending_expectation=self.expectation(
                                    v, v.pending, False
                                )
                                if v.pending
                                and (
                                    not v.applied
                                    or v.pending["plan_version"]
                                    > v.applied["plan_version"]
                                )
                                else None,
                                outstanding=list(v.radio_frames.values()),
                            )
                            for v in self.videos.values()
                            if v.state not in TERMINAL or v.radio_frames
                        ],
                    )
                    self.contextual(message)
                    try:
                        await self.rpc.call(message, self.cfg.awareness_timeout_s)
                    except Exception:
                        self.sync_needed = True
                        raise
                    self.synced_awareness_revision = max(
                        self.synced_awareness_revision, snapshot_revision
                    )
                priority, sequence, message = await self.awareness.get()
                # Arming may request a sync while this task is blocked above.
                # Recheck the fence before sending the message that woke it.
                if self.sync_needed:
                    await self.awareness.put((priority, sequence, message))
                    continue
                # The successful snapshot already represented older queued updates.
                if (
                    message.get("awareness_revision", 0)
                    <= self.synced_awareness_revision
                ):
                    continue
                try:
                    await self.rpc.call(message, self.cfg.awareness_timeout_s)
                except Exception:
                    # Preserve the failed message until sync and retry. Receiver never waits here.
                    self.sync_needed = True
                    self.awareness_seq += 1
                    await self.awareness.put((0, self.awareness_seq, message))
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self.record.event("awareness_unavailable", reason=str(ex))
                await asyncio.sleep(0.2)

    async def resource_reports(self):
        interval = self.cfg.ran_telemetry["report_interval_ms"] / 1000
        start = time.time_ns()
        target = time.monotonic() + interval
        sequence = 0
        while True:
            await asyncio.sleep(max(0, target - time.monotonic()))
            end = time.time_ns()
            sequence += 1
            report = dict(
                sequence=sequence,
                window_start_unix_ns=start,
                window_end_unix_ns=end,
                snapshot_monotonic_ns=time.monotonic_ns(),
                scope="host-submitted workload; not kernel residency",
                streams=[
                    dict(
                        application=a,
                        priority=p,
                        queued=sum(
                            1
                            for key in items
                            if key in self.frames and self.frames[key].state == "QUEUED"
                        ),
                        inflight=[
                            dict(
                                video_id=key[0],
                                frame_id=key[1],
                                ue_id=self.videos[key[0]].uid,
                                **context,
                            )
                            for key, context in self.inflight.items()
                            if context["application"] == a and context["priority"] == p
                        ],
                    )
                    for (a, p), items in self.pending.items()
                ],
                tracker_active=[v.vid for v in self.videos.values() if v.tracker_busy],
                workers=self.workers.ready,
                # Aetheredge: NVML supplies device load, not CUDA kernel residency.
                gpu=self.gpu.sample(),
            )
            self.aware("mec_resources", **report)
            self.record.event("mec_resources", snapshot=report)
            start = end
            target += interval
            if target < time.monotonic():
                target = time.monotonic() + interval

    # Source changes request recovery; report sequence numbers reject stale repeats.
    async def receive_reports(self):
        source = None
        while True:
            try:
                request = self.contextual(envelope("subscribe", self.cfg.run_id))
                async for r in self.rpc.stream(request):
                    if r.get("run_id") != self.cfg.run_id:
                        continue
                    expected = self.join_context()
                    actual = {key: r.get(key) for key in expected}
                    if actual != expected:
                        self.record.event(
                            "radio_report_context_mismatch", report_context=actual
                        )
                        continue
                    if r["source_generation"] != source:
                        source = r["source_generation"]
                        self.sync_needed = True
                    if (
                        self.last_report
                        and source == self.last_report["source_generation"]
                        and r["sequence"] <= self.last_report["sequence"]
                    ):
                        continue
                    self.last_report = r
                    self.report_received = time.monotonic()
                    for uid, m in r.get("ues", {}).items():
                        if uid in self.ues:
                            self.usable_radio(uid)
                            self.send(
                                uid,
                                envelope(
                                    "association_status",
                                    self.cfg.run_id,
                                    ue_id=uid,
                                    valid=bool(
                                        m["association_valid"]
                                        and m.get("connection_id")
                                        == self.ues[uid].connection_id
                                    ),
                                ),
                            )
                    for cmd in r.get("probe_commands", []):
                        if cmd["ue_id"] in self.ues:
                            u = self.ues[cmd["ue_id"]]
                            if cmd["connection_id"] == u.connection_id:
                                self.send(
                                    u.uid,
                                    envelope(
                                        "association_probe",
                                        self.cfg.run_id,
                                        endpoint=dict(
                                            host=self.cfg.advertised_ip,
                                            port=self.cfg.control_port + 1,
                                        ),
                                        **cmd,
                                    ),
                                )
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self.record.event("reports_unavailable", reason=str(ex))
                self.sync_needed = True
                await asyncio.sleep(0.5)

    async def close(self):
        self.closed = True
        for v in self.videos.values():
            if v.state not in TERMINAL:
                self.close_input(v, "server_shutdown")
        await asyncio.to_thread(self.workers.close)
        until = time.monotonic() + 0.5
        while self.inflight and time.monotonic() < until:
            await asyncio.sleep(0.005)
        for f in list(self.frames.values()):
            self.fail_frame(f.vid, f.fid, "server_shutdown")
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
        self.snapshot()
        await self.rpc.close()
        self.gpu.close()
        await asyncio.to_thread(self.record.close)
