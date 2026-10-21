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
from .config import qualified
from .statistics import History, NetworkFilter
from .policies import HeuristicGoP, HeuristicPriority, validate_joint
from .recording import Recorder, ReplayStore
from .media import Receiver, MockReceiver
from .workers import Workers, PRIORITIES
from .tracking import Tracker
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
    sender: object = None
    generation: int = 0
    association_revision: int = 0
    disconnect_at: float | None = None
    network: object = None
    clock_uncertainty_ns: int | None = None
    clock_mapping: dict = field(default_factory=dict)


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
    finalized: bool = False
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


class Engine:
    def __init__(self, cfg, catalog, profiles, workers=None):
        self.cfg = cfg
        self.catalog = catalog
        self.profiles = profiles
        self.candidates = qualified(catalog, profiles)
        self.root = Path(cfg.runs_dir) / cfg.run_id / "mec"
        self.record = Recorder(self.root)
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
            for a in ("tracking", "detection", "segmentation")
            for p in PRIORITIES
        }
        self.histories = {}
        self.residence = {}
        self.queue_history = History(cfg.global_queue_initial_s)
        self.policy = HeuristicGoP()
        self.priority = HeuristicPriority()
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
                    self.fail_frame, d.video_id, d.frame_id, "buffer_overflow"
                )

    def send(self, uid, message):
        u = self.ues[uid]
        if u.connected and u.sender:
            u.sender(message)

    def aware(self, kind, priority=1, **value):
        self.awareness_seq += 1
        msg = envelope(kind, self.cfg.run_id, **value)
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
        u.network = NetworkFilter(self.profiles["network"])
        u.network.reset(time.monotonic())
        self.association(u)
        self.record.event(
            "connected",
            ue_id=uid,
            connection_id=u.connection_id,
            generation=u.generation,
        )
        return u, envelope(
            "mec_hello",
            self.cfg.run_id,
            ue_id=uid,
            resume_token=token,
            connection_id=u.connection_id,
            catalog_version=self.catalog["version"],
            supported_configurations=list(self.candidates.values()),
            active_video_id=u.active_video,
            server_unix_ns=time.time_ns(),
            schema_version_control=1,
        )

    def disconnect(self, uid, generation):
        u = self.ues[uid]
        if u.generation != generation:
            return
        u.connected = False
        u.sender = None
        u.disconnect_at = time.monotonic()
        self.record.event("disconnected", ue_id=uid, connection_id=u.connection_id)

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
            self.association(u)
            u.clock_uncertainty_ns = m.get("clock_uncertainty_ns")
            u.clock_mapping = m.get("clock_mapping", {})
            return envelope("association_pending", self.cfg.run_id, ue_id=u.uid)
        if kind == "probe_report":
            self.record.event("ue_probe_report", ue_id=u.uid, report=m)
            return envelope("probe_report_recorded", self.cfg.run_id, ue_id=u.uid)
        if kind == "plan_unapplied":
            self.record.event("plan_unapplied", ue_id=u.uid, report=m)
            return None
        if kind not in ("video_start", "video_end"):
            raise ValueError("unknown control command")
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
            and len(self.workers.ready) == 3
            and bool(self.candidates)
            and not self.worker_failures
            and not self.record.error,
            backend=self.cfg.backend,
            experimental=self.cfg.mode == "experimental",
            workers=self.workers.ready,
            worker_failures=self.worker_failures,
            recording_error=self.record.error,
            qualified_candidates=len(self.candidates),
        )

    def usable_radio(self, uid, admission=False):
        if self.cfg.mode == "development":
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

    def choose(self, videos):
        us = {}
        cs = {
            cid: dict(c, processing_s=self.processing(cid))
            for cid, c in self.candidates.items()
            if cid not in self.quarantined
        }
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
                return None
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
                return None
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
        self.record.event(
            "gop_decision",
            snapshot_id=str(uuid.uuid4()),
            policy_version=self.policy.version,
            proposed=proposal,
            validated=bool(
                proposal is not None
                and validate_joint(
                    proposal,
                    us,
                    cs,
                    self.cfg.radio_budget,
                    self.cfg.compute_budget_s_per_s,
                )
            ),
        )
        return proposal

    async def start_video(self, u, m):
        async with self.admission_lock:
            seq = m["client_seq"]
            vid = str(uuid.uuid4())

            def denied(reason):
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

            if u.active_video and self.videos[u.active_video].state not in TERMINAL:
                return denied("active_video_exists")
            if not self.readiness()["ready"]:
                return denied("not_ready")
            if not self.usable_radio(u.uid, admission=True):
                return denied("usable_telemetry_required")
            if m["application"] not in ("detection", "tracking", "segmentation"):
                return denied("unsupported_application")
            if (
                sum(v.state not in TERMINAL for v in self.videos.values())
                >= self.cfg.max_videos
            ):
                return denied("video_capacity")
            fmt = m.get("input", {})
            if (fmt.get("width", 1280), fmt.get("height", 720), fmt.get("fps", 30)) != (
                1280,
                720,
                30,
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
                self.record.event(
                    "admission_denied",
                    ue_id=u.uid,
                    video_id=vid,
                    reason="heuristic_search_failed",
                )
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
            return envelope(
                "video_response",
                self.cfg.run_id,
                ue_id=u.uid,
                client_seq=seq,
                video_id=vid,
                accepted=True,
                status="awaiting_delivery",
                media_epoch=v.epoch,
                endpoint=dict(host=self.cfg.advertised_ip, port=port),
                initial_frame_id=0,
                slo_ms=v.slo * 1000,
                latency_origin="transmission",
                config=v.pending,
                rtp=dict(
                    extension_id=1, descriptor_bytes=96, mtu=1200, payload_type=96
                ),
                queue_contract=dict(
                    max_pending_frames=self.cfg.pending_frames,
                    max_pending_bytes=self.cfg.pending_bytes_per_video,
                    mandatory_drops=[
                        "nonpositive_slack",
                        "deadline_expired",
                        "buffer_overflow",
                        "session_suspended",
                        "transport_loss",
                        "tracking_cutoff",
                    ],
                ),
            )

    def plan(self, vs, proposal):
        self.plan_version += 1
        members = sorted(vs, key=lambda v: (v.admitted, v.uid))
        n = len(members)
        now = time.time_ns()
        activation = math.ceil(
            (now + self.cfg.gop_lead_s * 1e9 - self.epoch_unix_ns) / 1e9
        )
        for i, v in enumerate(members):
            self.config_version += 1
            cid = proposal[v.uid]
            c = self.candidates[cid]
            offset = i * (30 // n)
            boundary = (
                self.epoch_unix_ns
                + activation * 1_000_000_000
                + round(offset / 30 * 1e9)
            )
            cfg = dict(
                command_id=str(uuid.uuid4()),
                catalog_version=self.catalog["version"],
                config_version=self.config_version,
                plan_version=self.plan_version,
                activation_cycle=activation,
                boundary_unix_ns=boundary,
                epoch_unix_ns=self.epoch_unix_ns,
                offset_frames=offset,
                gop_frames=30,
                fps=30,
                width=1280,
                height=720,
                bitrate_bps=c["bitrate_bps"],
                model=c["model"],
                candidate_id=cid,
                input_hw=c["input_hw"],
                precision=c["precision"],
            )
            v.configs[self.config_version] = c
            v.plans[self.plan_version] = cfg
            v.pending = cfg
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
            )
        self.last_members = tuple(v.vid for v in members)

    def expectation(self, v, cfg, applied):
        c = self.candidates[cfg["candidate_id"]]
        res = self.residence_for(v, c["id"]).value
        return dict(
            applied=applied,
            config_version=cfg["config_version"],
            plan_version=cfg["plan_version"],
            fps=30,
            gop_frames=30,
            epoch_unix_ns=cfg["epoch_unix_ns"],
            offset_frames=cfg["offset_frames"],
            activation_cycle=cfg["activation_cycle"],
            first_tx_unix_ns=cfg.get("first_tx_unix_ns", cfg["boundary_unix_ns"]),
            first_frame_id=cfg.get("first_frame_id", 0),
            network_budget_ns=round((v.slo - res) * 1e9),
            size_profile={"I": c["frame_bytes_I"], "P": c["frame_bytes_P"]},
        )

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
            self.residence[key] = History(self.candidates[cid]["residence_s"], "mean")
        return self.residence[key]

    def end_video(self, u, m):
        v = self.videos.get(m["video_id"])
        if not v or v.uid != u.uid:
            raise ValueError("video identity mismatch")
        final = integer(m["final_frame_id"])
        v.final_id = final
        v.state = "DRAINING"
        v.input_close_at = time.monotonic() + self.cfg.final_arrival_s
        return envelope(
            "video_end_ack",
            self.cfg.run_id,
            ue_id=u.uid,
            video_id=v.vid,
            client_seq=m["client_seq"],
        )

    def receive_descriptor(self, d, first_ns):
        v = self.videos.get(d.video_id)
        if (
            not v
            or v.epoch != d.media_epoch
            or v.state in TERMINAL
            or v.resolved.has(d.frame_id)
        ):
            return
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
        f = Frame(v.vid, d.frame_id, d, first_ns, c)
        self.frames[key] = f
        if v.state == "AWAITING_DELIVERY":
            v.state = "ACTIVE"
        v.received += 1
        if not v.applied or d.config_version != v.applied["config_version"]:
            plan = dict(plan, first_frame_id=d.frame_id, first_tx_unix_ns=d.tx_unix_ns)
            v.applied = plan
            v.current = plan
            self.record.event(
                "configuration_observed",
                video_id=v.vid,
                frame_id=d.frame_id,
                config_version=d.config_version,
                plan_version=d.plan_version,
            )
        u = self.ues[v.uid]
        if v.applied and d.config_version == v.applied["config_version"]:
            plan = v.applied
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
            applied_expectation=dict(
                self.expectation(v, plan, True),
                first_frame_id=plan.get("first_frame_id", d.frame_id),
                first_tx_unix_ns=plan.get("first_tx_unix_ns", d.tx_unix_ns),
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
                report = self.last_report or {}
                r = report.get("ues", {}).get(v.uid, {})
                valid = bool(
                    self.cfg.allow_scheduled_throughput_estimate
                    and r.get("throughput_valid")
                    and time.time_ns() - report.get("window_end_unix_ns", 0)
                    <= 300_000_000
                )
                self.ues[v.uid].network.observe(
                    (v.vid, d.frame_id),
                    d.encoded_bytes,
                    r.get("throughput_bytes_s") or 0,
                    f.config["bitrate_bps"],
                    time.monotonic(),
                    valid,
                )
                u = self.ues[v.uid]
                u.network.propagate(received_ns / 1e9)
                f.network_s = f.config["bitrate_bps"] / 1e6 * u.network.mu
                f.deadline_ns = f.complete_ns + round((v.slo - f.network_s) * 1e9)
            self.flush_progress((v.vid, d.frame_id))
            del v.radio_frames[d.frame_id]

    def flush_progress(self, key):
        msg = self.progress.pop(key, None)
        if msg:
            self.aware("progress", 0 if msg["complete"] else 1, **msg)
            self.last_progress[key] = time.monotonic()

    def decoded(self, d, rgb, decoded_ns):
        f = self.frames.get((d.video_id, d.frame_id))
        v = self.videos.get(d.video_id)
        if not f or not v or v.resolved.has(d.frame_id):
            return
        if f.complete_ns is None:
            self.fail_frame(v.vid, d.frame_id, "incomplete_encoded_access_unit")
            return
        if v.input_closed:
            self.fail_frame(v.vid, d.frame_id, "input_closed")
            return
        if (
            v.state == "SUSPENDED"
            and v.suspension_boundary
            and d.tx_unix_ns >= v.suspension_boundary
        ):
            self.fail_frame(v.vid, d.frame_id, "session_suspended")
            return
        f.state = "PRIORITY_PENDING"
        f.decoded_ns = decoded_ns
        f.rgb = rgb
        f.times["decode_s"] = (decoded_ns - f.complete_ns) / 1e9
        # N and L were cached at complete AU admission; decoding consumes L-now.
        waiting = max(0, (time.monotonic_ns() - f.complete_ns) / 1e9)
        f.priority = self.priority.propose(
            v.slo,
            f.network_s,
            waiting,
            self.queue_history.value,
            self.processing(f.config["id"]),
        )
        if f.priority is None:
            self.fail_frame(v.vid, d.frame_id, "nonpositive_slack")
            return
        pending = [
            x
            for x in self.frames.values()
            if x.vid == v.vid and x.state in ("PRIORITY_PENDING", "QUEUED")
        ]
        while (
            len(pending) > self.cfg.pending_frames
            or sum(x.rgb.nbytes for x in pending if x.rgb is not None)
            > self.cfg.pending_bytes_per_video
        ):
            oldest = min(pending, key=lambda x: x.fid)
            pending.remove(oldest)
            self.fail_frame(v.vid, oldest.fid, "buffer_overflow")
        if v.resolved.has(d.frame_id):
            return
        f.queue_ns = time.monotonic_ns()
        f.state = "QUEUED"
        self.pending[(v.app, f.priority)].append((v.vid, f.fid))
        self.record.event(
            "priority",
            video_id=v.vid,
            frame_id=f.fid,
            priority=f.priority,
            network_s=f.network_s,
            waiting_s=waiting,
        )

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
            if reason in ("execution_failed", "transport_loss", "invalid_metadata")
            else "dropped",
            reason,
        )

    def resolve_missing(self, v, fid, reason):
        if v.resolved.has(fid):
            return
        v.resolved.add(fid)
        v.counters["failed"] += 1
        m = envelope(
            "frame_result",
            self.cfg.run_id,
            ue_id=v.uid,
            video_id=v.vid,
            request_id=f"{v.vid}/{fid}",
            frame_id=fid,
            result_id=result_id(v.vid, fid, "failed"),
            gop_id=None,
            config_version=None,
            plan_version=None,
            application=v.app,
            model=None,
            priority=None,
            outcome="failed",
            reason=reason,
            stage_times={},
            payload=None,
            loss_detected_at_unix_ns=time.time_ns(),
            metadata_available=False,
        )
        self.task(self.deliver(v, m, None, time.monotonic_ns(), None))

    def resolve(self, f, outcome, reason=None, payload=None):
        v = self.videos[f.vid]
        if v.resolved.has(f.fid):
            return
        v.resolved.add(f.fid)
        v.counters[outcome] += 1
        rid = result_id(v.vid, f.fid, outcome)
        now = time.monotonic_ns()
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
            model=f.config["model"],
            priority=f.priority,
            outcome=outcome,
            reason=reason,
            stage_times=f.times,
            payload=payload,
        )
        f.rgb = None
        self.frames.pop((f.vid, f.fid), None)
        v.tracking_ready.pop(f.fid, None)
        self.task(self.deliver(v, message, f.complete_ns, now, f.config["id"]))

    async def deliver(self, v, message, complete_ns, ready_ns, cid):
        begin = time.monotonic_ns()
        raw = await asyncio.to_thread(dumps, message)
        message["stage_times"]["result_prepare_s"] = (time.monotonic_ns() - begin) / 1e9
        self.record.put(
            "jobs.jsonl", {k: value for k, value in message.items() if k != "payload"}
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
                    f.state == "RECEIVING"
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
                        self.quarantined.add(f.config["id"])
                        self.fail_frame(vid, fid, "execution_failed")
                        continue
                    v = self.videos[vid]
                    f.times.update(m["times"])
                    wait = (m["worker_start_ns"] - f.queue_ns) / 1e9
                    f.times["queue_wait_s"] = wait
                    f.payload = m["payload"]
                    f.rgb = None
                    if v.app == "tracking":
                        f.state = "REORDER_WAIT"
                        f.detection_ready_ns = ns
                        v.tracking_ready[fid] = f
                        while len(v.tracking_ready) > self.cfg.tracker_buffer:
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
                    if ns >= f.deadline_ns:
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
                if (
                    v.input_close_at is not None
                    and now >= v.input_close_at
                    and not v.input_closed
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
                    if gaps_done and (not outstanding or now >= v.drain_until):
                        for f in outstanding:
                            self.fail_frame(v.vid, f.fid, "drain_timeout")
                        if v.state not in TERMINAL:
                            v.state = "COMPLETED"
                        u.active_video = None
                        v.finalized = True
                        self.send(
                            v.uid,
                            envelope(
                                "video_status",
                                self.cfg.run_id,
                                video_id=v.vid,
                                status=v.state,
                                counters=v.counters,
                            ),
                        )
            cycle = (time.time_ns() - self.epoch_unix_ns) // 1_000_000_000
            if cycle != self.last_cycle and not self.closed:
                self.last_cycle = cycle
                self.coordinate()
            if now - self.last_snapshot >= self.cfg.snapshot_s:
                self.last_snapshot = now
                self.snapshot()
            await asyncio.sleep(0.002)

    def tracker_tick(self, v, ns):
        if v.tracker_busy:
            return
        fs = sorted(
            [f for f in self.frames.values() if f.vid == v.vid], key=lambda f: f.fid
        )
        ready = sorted(v.tracking_ready)
        if not ready:
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

    def close_input(self, v, reason):
        if v.input_closed:
            return
        v.input_closed = True
        v.drain_until = time.monotonic() + self.cfg.drain_s
        if v.receiver:
            self.task(asyncio.to_thread(v.receiver.close))
        if reason != "video_end":
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

    def coordinate(self):
        active = [
            v
            for v in self.videos.values()
            if v.state in ("ACTIVE", "AWAITING_DELIVERY")
        ]
        if any(not self.usable_radio(v.uid) for v in active):
            self.record.event("gop_frozen", reason="telemetry_unusable")
            return
        candidates = active[:]
        while candidates:
            proposal = self.choose(candidates)
            if proposal is not None:
                break
            newest = max(candidates, key=lambda v: v.admitted)
            candidates.remove(newest)
            newest.state = "SUSPENDED"
            boundary = time.time_ns() + round(self.cfg.gop_lead_s * 1e9)
            newest.suspension_boundary = boundary
            self.send(
                newest.uid,
                envelope(
                    "video_status",
                    self.cfg.run_id,
                    video_id=newest.vid,
                    status="SUSPENDED",
                    effective_unix_ns=boundary,
                    reason="joint_budget_search_failed",
                ),
            )
        if candidates:
            # Reconsider suspended sessions oldest-first, with the same joint validator.
            for v in sorted(
                [v for v in self.videos.values() if v.state == "SUSPENDED"],
                key=lambda v: v.admitted,
            ):
                if not self.usable_radio(v.uid, admission=True):
                    continue
                test = self.choose(candidates + [v])
                if test is not None:
                    candidates.append(v)
                    proposal = test
                    v.state = "ACTIVE"
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

    async def send_awareness(self):
        while True:
            try:
                if self.sync_needed:
                    message = envelope(
                        "sync",
                        self.cfg.run_id,
                        associations=[
                            dict(
                                ue_id=u.uid,
                                connection_id=u.connection_id,
                                ip=u.ip,
                                connection_generation=u.generation,
                                association_revision=u.association_revision,
                            )
                            for u in self.ues.values()
                        ],
                        sessions=[
                            dict(
                                ue_id=v.uid,
                                video_id=v.vid,
                                media_epoch=v.epoch,
                                input_closed=v.input_closed,
                                expectation=self.expectation(v, v.applied, True)
                                if v.applied
                                else None,
                                outstanding=list(v.radio_frames.values()),
                            )
                            for v in self.videos.values()
                            if v.state not in TERMINAL or v.radio_frames
                        ],
                    )
                    await self.rpc.call(message, self.cfg.awareness_timeout_s)
                    self.sync_needed = False
                _, _, message = await self.awareness.get()
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
            self.aware(
                "mec_resources",
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
            )
            start = end
            target += interval
            if target < time.monotonic():
                target = time.monotonic() + interval

    async def receive_reports(self):
        source = None
        while True:
            try:
                async for r in self.rpc.stream(envelope("subscribe", self.cfg.run_id)):
                    if r.get("run_id") != self.cfg.run_id:
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
        await asyncio.to_thread(self.record.close)
