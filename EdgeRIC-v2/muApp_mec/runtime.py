from __future__ import annotations
import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from dataclasses import asdict
from .association import Associations
from .demand import Ledger
from .heuristic import weights
from .telemetry import Window
from mec_contracts.rpc import add_service
from mec_contracts.wire import integer


# Own radio association and demand locally; MEC retains application execution authority.
class Runtime:
    def __init__(self, config, adapter=None):
        self.cfg = config
        self.adapter = adapter
        self.run_id = None
        self.campaign_id = None
        self.scenario_id = None
        self.attempt_id = None
        self.source = str(uuid.uuid4())
        self.associations = Associations(config["association"])
        self.sessions = {}
        self.retired_sessions = {}
        self.sync_revision = -1
        self.awareness_generation = None
        self.retired_awareness_generations = set()
        self.metrics = {}
        # Aetheredge: RAN capacity and grant accounting now arrive over ZMQ.
        self.radio_generation = None
        self.radio_sequence = None
        self.radio_counters = {}
        self.capacity_samples = {}
        self.radio_coverage_valid = False
        self.latest_report = None
        self.sequence = 0
        self.capacity = {}
        self.latest_metric = 0.0
        self.root = Path(config["runs_dir"]) / self.source
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_queue = asyncio.Queue(maxsize=16384)
        self.log_errors = 0
        self.last_index = 0
        telemetry = config.get("ran_telemetry", {})
        window_ms = telemetry.get("aggregation_window_ms", 100)
        interval_ms = telemetry.get("report_interval_ms", 100)
        if window_ms != 100 or interval_ms != 100:
            raise ValueError(
                "initial validated reporting contract requires 100-ms windows/reports"
            )
        self.window = None
        self.window_ns = window_ms * 1_000_000
        self.probe_commands = []
        # Aetheredge: Capacity tables and realized-ratio proxies are replaced by OAI TBS output.

    def event(self, kind, **data):
        try:
            self.log_queue.put_nowait(
                dict(
                    type=kind,
                    monotonic_ns=time.monotonic_ns(),
                    run_id=self.run_id,
                    campaign_id=self.campaign_id,
                    scenario_id=self.scenario_id,
                    attempt_id=self.attempt_id,
                    **data,
                )
            )
        except asyncio.QueueFull:
            self.log_errors += 1

    async def writer(self):
        while True:
            data = await self.log_queue.get()
            try:
                await asyncio.to_thread(self._append, data)
            except Exception:
                self.log_errors += 1
            finally:
                self.log_queue.task_done()

    def _append(self, data):
        with (self.root / "events.jsonl").open("a") as f:
            f.write(json.dumps(data, allow_nan=False) + "\n")

    def _snapshot(self, data):
        temp = self.root / "associations.tmp"
        temp.write_text(json.dumps(data, indent=2))
        temp.replace(self.root / "associations.json")

    def join_context(self):
        return {
            "campaign_id": getattr(self, "campaign_id", None),
            "scenario_id": getattr(self, "scenario_id", None),
            "attempt_id": getattr(self, "attempt_id", None),
        }

    @staticmethod
    def message_context(message):
        context = {
            key: message.get(key)
            for key in ("campaign_id", "scenario_id", "attempt_id")
        }
        values = tuple(context.values())
        if any(value is not None for value in values) and not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError("incomplete scenario context")
        return context

    # Validate run/media identity before applying MEC awareness messages.
    async def exchange(self, m):
        if m.get("schema_version") != 1:
            raise ValueError("schema mismatch")
        run = m["run_id"]
        kind = m["type"]
        if kind == "sync":
            context = self.message_context(m)
            generation = m.get("awareness_generation")
            if not isinstance(generation, str) or not generation:
                raise ValueError("missing awareness generation")
            retired_generations = getattr(
                self, "retired_awareness_generations", set()
            )
            self.retired_awareness_generations = retired_generations
            if generation in retired_generations:
                return {"source_generation": self.source, "status": "stale_generation"}
            current_generation = getattr(self, "awareness_generation", None)
            generation_changed = self.run_id != run or current_generation != generation
            if generation_changed:
                if current_generation is not None:
                    retired_generations.add(current_generation)
                self.sessions.clear()
                self.retired_sessions.clear()
                self.associations = Associations(self.cfg["association"])
                self.run_id = run
                self.awareness_generation = generation
                self.sync_revision = -1
            revision = m.get("awareness_revision")
            if revision is not None:
                revision = integer(revision)
                if revision <= self.sync_revision:
                    return {"source_generation": self.source, "status": "stale"}
            context_changed = context != self.join_context()
            # Apply identity only after the revision fence; a delayed sync must
            # never relabel current radio evidence as an older attempt.
            self.campaign_id = context["campaign_id"]
            self.scenario_id = context["scenario_id"]
            self.attempt_id = context["attempt_id"]
            if context_changed:
                # Begin with a fresh radio window. Reacquire cumulative-counter
                # baselines instead of charging prior traffic to this attempt.
                self.radio_counters.clear()
                self.capacity_samples.clear()
                self.window = None
                self.latest_report = None
                self.radio_coverage_valid = False
                self.latest_metric = 0.0
                self.probe_commands.clear()
            # A full sync is authoritative for current sessions and actual radio demand.
            # Aetheredge: absence is authoritative, but an ordinary scenario
            # sync preserves valid RNTI leases for identities still present.
            snapshot_associations = m.get("associations", [])
            current_uids = {a["ue_id"] for a in snapshot_associations}
            for uid in set(self.associations.bindings) - current_uids:
                del self.associations.bindings[uid]
                self.associations.last_result.pop(uid, None)
            self.associations.pending = [
                uid for uid in self.associations.pending if uid in current_uids
            ]
            if (
                self.associations.probe
                and self.associations.probe["ue_id"] not in current_uids
            ):
                self.associations.probe = None
            for a in snapshot_associations:
                self.associations.register(
                    a["ue_id"],
                    a["connection_id"],
                    a["ip"],
                    a.get("connection_generation"),
                    a.get("association_revision", 0),
                )
            current = {s["video_id"] for s in m.get("sessions", [])}
            for vid in set(self.sessions) - current:
                del self.sessions[vid]
            for s in m.get("sessions", []):
                self._session(s, authoritative=True)
            if revision is not None:
                self.sync_revision = revision
                # The snapshot fence now rejects every message these tombstones covered.
                self.retired_sessions = {
                    vid: value
                    for vid, value in self.retired_sessions.items()
                    if value[2] > revision
                }
            self.event("synchronized", sessions=len(self.sessions))
            return {"source_generation": self.source, "status": "synced"}
        if run != self.run_id:
            raise ValueError("sync required for run")
        current_generation = getattr(self, "awareness_generation", None)
        if m.get("awareness_generation") != current_generation:
            raise ValueError("sync required for awareness generation")
        if self.message_context(m) != self.join_context():
            raise ValueError("scenario context mismatch")
        revision = m.get("awareness_revision")
        if revision is not None:
            revision = integer(revision)
            # Messages created before the latest snapshot are already represented by it.
            if revision <= self.sync_revision:
                return {"status": "stale", "source_generation": self.source}
        if kind == "association":
            self.associations.register(
                m["ue_id"],
                m["connection_id"],
                m["ip"],
                m.get("connection_generation"),
                m.get("association_revision", 0),
            )
        elif kind == "mec_resources":
            self.mec_resources = m
            self.event("mec_resources", snapshot=m)
        elif kind == "session":
            self._session(m)
        elif kind in ("descriptor", "progress", "reception_loss"):
            if m["video_id"] in self.retired_sessions:
                return {"status": "retired", "source_generation": self.source}
            s = self.sessions.get(m["video_id"])
            if (
                not s
                or s["ue_id"] != m["ue_id"]
                or s["media_epoch"] != m["media_epoch"]
            ):
                raise ValueError("obsolete media identity")
            ledger = s["ledger"]
            fid = integer(m["frame_id"])
            if kind == "descriptor":
                integer(m["encoded_bytes"], 1)
                # Preserve original frame config/deadline; no reassignment to newest GoP.
                ledger.descriptor(
                    fid,
                    m["tx_unix_ns"],
                    m.get("network_deadline_unix_ns"),
                    m["encoded_bytes"],
                    True,
                    m["config_version"],
                )
                if m.get("applied_expectation"):
                    self._apply_expectation(
                        s, m["applied_expectation"], fid, m["tx_unix_ns"]
                    )
            elif kind == "progress":
                ledger.progress(
                    fid,
                    integer(m["unique_bytes"]),
                    integer(m["progress_seq"]),
                    m.get("complete", False),
                )
                self._retire_session(m["video_id"], revision)
            else:
                self.event(
                    "reception_loss",
                    video_id=m["video_id"],
                    frame_id=fid,
                    reason=m.get("reason"),
                )
        elif kind == "probe_receipt":
            self.associations.receipt(
                m["ue_id"], m["probe_id"], m["interval_index"], m["bytes"]
            )
            # Timing/correlation evidence only. Probe bytes are never frame demand.
            self.event(
                "probe_receipt",
                **{k: v for k, v in m.items() if k not in ("type", "run_id")},
            )
        else:
            raise ValueError("unknown awareness message")
        return {"status": "ok", "source_generation": self.source}

    # Merge a session; snapshots additionally retire actual demand they no longer list.
    def _session(self, m, authoritative=False):
        vid = m["video_id"]
        retired = self.retired_sessions.get(vid)
        if retired:
            if retired[:2] != (m["ue_id"], m["media_epoch"]):
                raise ValueError("conflicting retired session identity")
            return
        s = self.sessions.get(vid)
        if s and (s["media_epoch"] != m["media_epoch"] or s["ue_id"] != m["ue_id"]):
            raise ValueError("conflicting session identity")
        if not s:
            if len(self.sessions) >= self.cfg.get("max_sessions", 64):
                raise ValueError("session ledger capacity")
            s = dict(
                ue_id=m["ue_id"],
                media_epoch=m["media_epoch"],
                ledger=Ledger(),
                next_prediction=0,
                input_closed=False,
            )
            self.sessions[vid] = s
        control = m.get("transmission_control")
        if control:
            version = integer(control["control_version"], 1)
            integer(control["effective_unix_ns"])
            stop = control.get("stop_unix_ns")
            if stop is not None:
                integer(stop)
            if control["status"] not in ("ACTIVE", "SUSPENDED"):
                raise ValueError("invalid transmission status")
            if control["status"] == "SUSPENDED" and stop != control["effective_unix_ns"]:
                raise ValueError("suspension boundary mismatch")
            old = s.get("transmission_control")
            if old and version == old["control_version"] and control != old:
                raise ValueError("conflicting transmission control version")
            if not old or version > old["control_version"]:
                s["transmission_control"] = dict(control)
                if stop is not None:
                    # Retract only unserved predictions, never actual/outstanding traffic.
                    ledger = s["ledger"]
                    for fid, frame in list(ledger.frames.items()):
                        if (
                            frame.start_ns >= stop
                            and not frame.actual
                            and not frame.confirmed
                            and not frame.estimated
                            and not frame.scheduled
                        ):
                            del ledger.frames[fid]
                            s["next_prediction"] = min(s["next_prediction"], fid)
        e = m.get("expectation")
        if e:
            if e.get("applied"):
                self._apply_expectation(s, e)
            else:
                self._pending_expectation(s, e)
        if m.get("pending_expectation"):
            self._pending_expectation(s, m["pending_expectation"])
        if "input_closed" in m:
            s["input_closed"] = bool(m["input_closed"])
            if s["input_closed"]:
                # Unserved predictions are not radio evidence after input closure.
                for fid, frame in list(s["ledger"].frames.items()):
                    if (
                        not frame.actual
                        and not frame.confirmed
                        and not frame.estimated
                        and not frame.scheduled
                    ):
                        del s["ledger"].frames[fid]
        outstanding = m.get("outstanding", [])
        current = set()
        for f in outstanding:
            current.add(f["frame_id"])
            s["ledger"].descriptor(
                f["frame_id"],
                f["tx_unix_ns"],
                f.get("network_deadline_unix_ns"),
                f["encoded_bytes"],
                True,
                f["config_version"],
            )
            s["ledger"].progress(
                f["frame_id"],
                f.get("unique_bytes", 0),
                f.get("progress_seq", 0),
                f.get("complete", False),
            )
        if authoritative:
            for fid, frame in list(s["ledger"].frames.items()):
                if frame.actual and fid not in current:
                    s["ledger"].retire(fid)
        self._retire_session(vid, m.get("awareness_revision"))

    @staticmethod
    def _expectation_key(e):
        return (e.get("plan_version", 0), e.get("config_version", 0))

    @staticmethod
    def _drop_untouched_predictions(s, expectation, start_fid=None):
        if not expectation:
            return
        config = expectation["config_version"]
        for fid, frame in list(s["ledger"].frames.items()):
            if (
                frame.config == config
                and (start_fid is None or fid >= start_fid)
                and not frame.actual
                and not frame.confirmed
                and not frame.estimated
                and not frame.scheduled
            ):
                del s["ledger"].frames[fid]

    # Retain only the newest future plan and retract superseded unserved estimates.
    def _pending_expectation(self, s, e):
        applied = s.get("applied_expectation")
        old = s.get("pending_expectation")
        key = self._expectation_key(e)
        if applied and key <= self._expectation_key(applied):
            return
        if old and key <= self._expectation_key(old):
            return
        self._drop_untouched_predictions(s, old)
        if old:
            s["next_prediction"] = min(
                s["next_prediction"], old.get("first_frame_id", 0)
            )
        if applied:
            boundary = e.get("first_frame_id", 0)
            self._drop_untouched_predictions(s, applied, boundary)
            s["next_prediction"] = min(s["next_prediction"], boundary)
        s["pending_expectation"] = dict(e)

    # Actual ordered evidence promotes a plan; late older frames retain their own config only.
    def _apply_expectation(self, s, e, fid=None, tx_ns=None):
        e = dict(e, applied=True)
        applied = s.get("applied_expectation")
        pending = s.get("pending_expectation")
        key = self._expectation_key(e)
        if applied and key < self._expectation_key(applied):
            return
        if pending and key < self._expectation_key(pending):
            boundary_fid = pending.get("first_frame_id", 0)
            boundary_ns = pending["first_tx_unix_ns"]
            # Old metadata sent before the boundary is merely delayed, not a missed switch.
            if fid is None or fid < boundary_fid or tx_ns is None or tx_ns < boundary_ns:
                return
            self._drop_untouched_predictions(s, pending)
            s.pop("pending_expectation", None)
            s["next_prediction"] = min(s["next_prediction"], boundary_fid)
            return
        if applied and key == self._expectation_key(applied):
            return
        s["applied_expectation"] = e
        s["next_prediction"] = max(
            s["next_prediction"], e.get("first_frame_id", fid or 0)
        )
        if pending and key >= self._expectation_key(pending):
            s.pop("pending_expectation", None)

    def _retire_session(self, vid, revision=None):
        s = self.sessions.get(vid)
        if not s or not s.get("input_closed") or s["ledger"].frames:
            return
        # Keep only identity and a revision fence; the full demand ledger is reusable capacity.
        revision = self.sync_revision if revision is None else revision
        self.retired_sessions[vid] = (s["ue_id"], s["media_epoch"], revision)
        del self.sessions[vid]
        self.event("session_retired", video_id=vid)

    # Aetheredge: Debit provisional service once across all frames sharing one RNTI.
    def _apply_new_data_service(self, rnti, tbs_bytes, now):
        fraction = self.cfg.get("payload_fraction")
        if fraction is None or tbs_bytes <= 0:
            return
        matching = []
        for s in self.sessions.values():
            binding = self.associations.valid(s["ue_id"], now)
            if binding and binding.rnti == rnti:
                matching.extend(s["ledger"].frames.values())
        if not matching:
            return
        ordered = sorted(matching, key=lambda f: (f.start_ns, f.frame_id))
        left = tbs_bytes * fraction
        for frame in ordered:
            used = min(left, frame.remaining)
            frame.estimated += used
            left -= used
            if left <= 0:
                break
        ordered[0].scheduled += tbs_bytes

    # Aetheredge: Convert one conflated ZMQ snapshot into capacity and counter deltas.
    def _consume_ran_sample(self, now):
        generation = getattr(self.adapter, "source_generation", None)
        sequence = getattr(self.adapter, "report_sequence", None)
        if generation is not None and generation != self.radio_generation:
            previous_generation = self.radio_generation
            self.radio_generation = generation
            self.radio_sequence = None
            self.radio_counters.clear()
            self.capacity_samples.clear()
            self.capacity.clear()
            self.radio_coverage_valid = False
            # Aetheredge: the first observed generation establishes the radio
            # baseline; it does not replace any prior radio identity. Invalidating
            # an in-progress first probe here changes its binding generation and
            # rejects otherwise conclusive evidence. A later source replacement
            # still invalidates every mapping immediately.
            if previous_generation is not None:
                for uid in list(self.associations.bindings):
                    self.associations.invalidate(uid, "ran_source_generation")
            self.event("radio_source_generation", generation=generation)
        if sequence is not None:
            if self.radio_sequence is not None and sequence <= self.radio_sequence:
                return
            self.radio_sequence = sequence

        counters_valid = True
        for rnti, metric in self.metrics.items():
            # Aetheredge: Repeated latest-value capacity does not refresh its measurement age.
            capacity_key = (
                generation,
                metric.get("capacity_source_frame"),
                metric.get("capacity_source_slot"),
                metric.get("capacity_target_frame"),
                metric.get("capacity_target_slot"),
            )
            capacity = metric.get("capacity_bytes_per_prb", 0)
            if (
                metric.get("capacity_valid")
                and capacity > 0
                and self.capacity_samples.get(rnti) != capacity_key
            ):
                self.capacity_samples[rnti] = capacity_key
                self.capacity[rnti] = (capacity, now)

            # Aetheredge: Baseline the first cumulative sample; debit only later new data.
            current = (
                metric.get("new_data_prbs_total", 0),
                metric.get("new_data_tbs_bytes_total", 0),
                metric.get("retransmission_prbs_total", 0),
                metric.get("retransmission_tbs_bytes_total", 0),
                metric.get("dl_new_data_prbs_total", 0),
                metric.get("dl_new_data_tbs_bytes_total", 0),
                metric.get("dl_retransmission_prbs_total", 0),
                metric.get("dl_retransmission_tbs_bytes_total", 0),
            )
            previous = self.radio_counters.get(rnti)
            self.radio_counters[rnti] = current
            if previous is None:
                continue
            delta = tuple(value - old for value, old in zip(current, previous))
            if any(value < 0 for value in delta):
                counters_valid = False
                continue
            if any(delta):
                self.window.add_counters(rnti, *delta)
                # Aetheredge: Retransmission deltas are telemetry only in this initial model.
                self._apply_new_data_service(rnti, delta[1], now)
        self.radio_coverage_valid = counters_valid

    async def reports(self, m):
        previous = -1
        while True:
            if self.latest_report and previous != self.sequence:
                previous = self.sequence
                # No RNTI/IP appears in reports sent to MEC. Association status is UE-centric.
                value = dict(self.latest_report)
                value["probe_commands"] = self.probe_commands[:]
                self.probe_commands.clear()
                yield value
            await asyncio.sleep(0.01)

    # Generate provisional demand from the applied schedule or its advertised successor.
    def predict(self, now_ns):
        for s in self.sessions.values():
            if s.get("input_closed"):
                continue
            applied = s.get("applied_expectation")
            pending = s.get("pending_expectation")
            plans = []
            if applied:
                horizon = (
                    min(now_ns, pending["first_tx_unix_ns"] - 1)
                    if pending
                    else now_ns
                )
                plans.append((applied, horizon))
            if pending:
                plans.append((pending, now_ns))
            for e, horizon in plans:
                start = e["first_tx_unix_ns"]
                fps = e["fps"]
                base = e.get("first_frame_id", 0)
                if e is pending:
                    s["next_prediction"] = max(s["next_prediction"], base)
                control = s.get("transmission_control") or {}
                stop = control.get("stop_unix_ns")
                resumed = (
                    control.get("status") == "ACTIVE"
                    and e.get("control_version", 0)
                    > control.get("suspension_version", 0)
                    and start >= (stop or 0)
                )
                if stop is not None and not resumed:
                    horizon = min(horizon, stop - 1)
                if horizon < start:
                    continue
                last = base + (horizon - start) * fps // 1_000_000_000
                # Bound catch-up rather than cancelling uncertain radio demand.
                if last - s["next_prediction"] > 120:
                    s["ledger"].valid = False
                    continue
                while s["next_prediction"] <= last:
                    fid = s["next_prediction"]
                    s["next_prediction"] += 1
                    kind = "I" if (fid - base) % e["gop_frames"] == 0 else "P"
                    size = e["size_profile"].get(kind)
                    if not size:
                        continue
                    tx = start + int((fid - base) * 1e9 / fps)
                    try:
                        s["ledger"].descriptor(
                            fid,
                            tx,
                            tx + e["network_budget_ns"],
                            int(size),
                            False,
                            e["config_version"],
                        )
                    except ValueError as ex:
                        self.event("demand_rejected", reason=str(ex))

    # Use current local state for radio actions; never await a MEC decision in this loop.
    async def run(self):
        import grpc

        server = grpc.aio.server(
            options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)]
        )
        add_service(server, self.exchange, self.reports)
        server.add_insecure_port(self.cfg["listen"])
        await server.start()
        writer = asyncio.create_task(self.writer())
        next_report = time.time_ns()
        try:
            while True:
                now = time.monotonic()
                unix = time.time_ns()
                if self.window is None:
                    start = unix // self.window_ns * self.window_ns
                    self.window = Window(start, start + self.window_ns, "oai_zmq_cumulative_v1")
                if self.adapter:
                    try:
                        sample = self.adapter.receive()
                    except Exception as ex:
                        self.event("metric_error", reason=str(ex))
                        sample = None
                    if sample:
                        self.last_index, self.metrics = sample
                        self.latest_metric = now
                        self.associations.sample(now, self.metrics)
                        # Aetheredge: Consume capacity and grants from the same nonblocking sample.
                        self._consume_ran_sample(now)
                self.associations.tick(
                    now, set(self.metrics) if now - self.latest_metric < 0.3 else set()
                )
                command = self.associations.next_probe(now)
                if command:
                    self.probe_commands.append(command)
                    self.event("probe_command", command=command)
                self.predict(unix)
                # Aetheredge: CSV grant polling was removed; ZMQ counters are authoritative.
                while unix >= self.window.end_ns:
                    self.publish(now)
                    self.window = Window(
                        self.window.end_ns,
                        self.window.end_ns + self.window_ns,
                        "oai_zmq_cumulative_v1",
                    )
                snapshots = {}
                for s in self.sessions.values():
                    snap = s["ledger"].snapshot(unix)
                    uid = s["ue_id"]
                    if not snap["oldest"]:
                        continue
                    if uid not in snapshots:
                        snapshots[uid] = snap
                        continue
                    old = snapshots[uid]
                    old["valid"] &= snap["valid"]
                    old["count"] += snap["count"]
                    old["remaining_bytes"] += snap["remaining_bytes"]
                    old["overdue_bytes"] += snap["overdue_bytes"]
                    if snap["oldest"]["start_ns"] < old["oldest"]["start_ns"]:
                        old["oldest"] = snap["oldest"]
                    ds = [
                        x
                        for x in (
                            old["earliest_deadline_ns"],
                            snap["earliest_deadline_ns"],
                        )
                        if x is not None
                    ]
                    old["earliest_deadline_ns"] = min(ds) if ds else None
                caps = {}
                bindings = {}
                for uid in snapshots:
                    b = self.associations.valid(uid, now)
                    if b:
                        bindings[uid] = b
                        c = self.capacity.get(b.rnti)
                        if c and now - c[1] <= self.cfg["capacity_max_age_s"]:
                            caps[uid] = c[0]
                w, detail = weights(snapshots, caps, unix, **self.cfg["utility"])
                # Follow existing metric-triggered cadence. Strict k/k+1 enforcement is deferred.
                if self.adapter and sample:
                    action = {bindings[u].rnti: v for u, v in w.items()}
                    if self.cfg["mode"] == "heuristic":
                        self.adapter.send(self.last_index, action)
                    if self.cfg.get("log_weight_samples"):
                        self.event(
                            "weights",
                            shadow=self.cfg["mode"] == "shadow",
                            ran_index=self.last_index,
                            weights=w,
                            detail=detail,
                        )
                await asyncio.sleep(0.0002)
        finally:
            await server.stop(1)
            await self.log_queue.join()
            await asyncio.to_thread(
                self._snapshot,
                dict(
                    run_id=self.run_id,
                    source_generation=self.source,
                    bindings={
                        u: asdict(b) for u, b in self.associations.bindings.items()
                    },
                    matches=self.associations.last_result,
                ),
            )
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            if self.adapter:
                self.adapter.close()

    # Translate local RNTIs into UE-keyed reports before crossing the MEC boundary.
    def publish(self, now):
        record = self.window.record()
        allocated = record.pop("allocated_by_rnti")
        per = {}
        local_per = {}
        for uid in self.associations.bindings:
            b = self.associations.valid(uid, now)
            r = allocated.get(b.rnti, {}) if b else {}
            capacity = self.capacity.get(b.rnti) if b else None
            # Aetheredge: Use the newest grant telemetry even when capacity is unchanged.
            validation = self.metrics.get(b.rnti, {}) if b else {}
            def finite_value(key):
                value = validation.get(key)
                return value if not isinstance(value, float) or math.isfinite(value) else None

            rate = r.get("tbs_bytes", 0) / (self.window_ns / 1e9)
            fraction = self.cfg.get("payload_fraction")
            entry = dict(
                association_valid=bool(b),
                connection_id=self.associations.bindings[uid].connection_id,
                mapping_generation=self.associations.bindings[uid].generation,
                scheduled_prbs=r.get("prbs", 0),
                scheduled_tbs_bytes=r.get("tbs_bytes", 0),
                retransmission_prbs=r.get("retry_prbs", 0),
                retransmission_tbs_bytes=r.get("retry_tbs_bytes", 0),
                dl_scheduled_prbs=r.get("dl_prbs", 0),
                dl_scheduled_tbs_bytes=r.get("dl_tbs_bytes", 0),
                dl_retransmission_prbs=r.get("dl_retry_prbs", 0),
                dl_retransmission_tbs_bytes=r.get("dl_retry_tbs_bytes", 0),
                throughput_bytes_s=rate * fraction if fraction is not None else None,
                throughput_kind="scheduled_payload_conversion_estimate",
                throughput_valid=bool(
                    b
                    and fraction is not None
                    and self.radio_coverage_valid
                    and self.window.events
                ),
                capacity_bytes_per_prb=capacity[0] if capacity else None,
                capacity_kind="OAI_reference_tbs",
                capacity_age_s=now - capacity[1] if capacity else None,
                radio_state={
                    key: finite_value(key)
                    for key in (
                        "radio_state_valid",
                        "radio_state_frame",
                        "radio_state_slot",
                        "scheduler_ul_mcs",
                        "scheduler_dl_mcs",
                        "ul_bler",
                        "dl_bler",
                        "pusch_snr_db",
                        "pucch_snr_db",
                        "link_quality_valid",
                    )
                },
                cumulative_grants={
                    key: validation.get(key)
                    for key in (
                        "new_data_prbs_total",
                        "new_data_tbs_bytes_total",
                        "retransmission_prbs_total",
                        "retransmission_tbs_bytes_total",
                        "dl_new_data_prbs_total",
                        "dl_new_data_tbs_bytes_total",
                        "dl_retransmission_prbs_total",
                        "dl_retransmission_tbs_bytes_total",
                    )
                },
                latest_ul_grant={
                    key: validation.get(key)
                    for key in (
                        "latest_grant_valid",
                        "latest_grant_retransmission",
                        "latest_grant_prbs",
                        "latest_grant_tbs_bytes",
                        "latest_grant_mcs",
                        "latest_grant_harq_round",
                        "latest_grant_target_frame",
                        "latest_grant_target_slot",
                    )
                },
                latest_dl_grant={
                    key: validation.get(key)
                    for key in (
                        "latest_dl_grant_valid",
                        "latest_dl_grant_retransmission",
                        "latest_dl_grant_prbs",
                        "latest_dl_grant_tbs_bytes",
                        "latest_dl_grant_mcs",
                        "latest_dl_grant_harq_round",
                        "latest_dl_grant_frame",
                        "latest_dl_grant_slot",
                    )
                },
                rlc_counters={
                    "valid": validation.get("rlc_counters_valid"),
                    "dl_buffer_bytes": validation.get("dl_rlc_buffer_bytes"),
                    "buffer_layer": "RLC",
                    "dl_interface_sdu_bytes_total": validation.get(
                        "dl_rlc_interface_sdu_bytes_total"
                    ),
                    "ul_interface_sdu_bytes_total": validation.get(
                        "ul_rlc_interface_sdu_bytes_total"
                    ),
                    "throughput_boundary": "MAC/RLC interface",
                },
                # Aetheredge: Temporary context supports independent capacity validation.
                capacity_validation={
                    key: validation.get(key)
                    for key in (
                        "reference_prbs",
                        "reference_tbs_bits",
                        "ul_mcs",
                        "ul_mcs_table",
                        "ul_qm",
                        "ul_code_rate_x10240",
                        "ul_layers",
                        "ul_start_symbol",
                        "ul_symbols",
                        "ul_dmrs_re_per_prb",
                        "ul_rb_overhead",
                        "ul_tb_scaling",
                        "ul_transform_precoding",
                        "latest_grant_valid",
                        "latest_grant_retransmission",
                        "latest_grant_prbs",
                        "latest_grant_tbs_bytes",
                        "latest_grant_mcs",
                        "latest_grant_harq_round",
                        "latest_grant_target_frame",
                        "latest_grant_target_slot",
                    )
                },
            )
            per[uid] = entry
            # C-RNTI remains local EdgeRIC evidence and is not sent to the MEC.
            local_per[uid] = dict(entry, rnti=b.rnti if b else None)
        self.sequence += 1
        self.latest_report = dict(
            schema_version=1,
            type="radio_report",
            run_id=self.run_id,
            **self.join_context(),
            sequence=self.sequence,
            source_generation=self.source,
            radio_generation=self.radio_generation,
            radio_report_sequence=self.radio_sequence,
            ran_index=self.last_index,
            **record,
            ues=per,
            trace_coverage_valid=self.radio_coverage_valid,
            metric_age_s=now - self.latest_metric,
        )
        self.event("radio_report", report=dict(self.latest_report, ues=local_per))
        if self.sequence % 50 == 0:
            data = dict(
                run_id=self.run_id,
                **self.join_context(),
                source_generation=self.source,
                bindings={u: asdict(b) for u, b in self.associations.bindings.items()},
                matches=self.associations.last_result,
            )
            asyncio.create_task(asyncio.to_thread(self._snapshot, data))
