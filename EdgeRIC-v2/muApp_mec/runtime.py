from __future__ import annotations
import asyncio
import json
import time
import uuid
from pathlib import Path
from dataclasses import asdict
from .association import Associations
from .demand import Ledger
from .heuristic import weights
from .telemetry import TraceReader, Window
from mec_contracts.rpc import add_service
from mec_contracts.wire import integer


class Runtime:
    def __init__(self, config, adapter=None):
        self.cfg = config
        self.adapter = adapter
        self.run_id = None
        self.source = str(uuid.uuid4())
        self.associations = Associations(config["association"])
        self.sessions = {}
        self.metrics = {}
        self.trace = TraceReader(config["trace_file"])
        self.latest_report = None
        self.sequence = 0
        self.capacity = {}
        self.latest_metric = 0.0
        self.last_trace_generation = 0
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
        self.capacity_table = {}
        if config.get("capacity_table"):
            import csv

            with open(config["capacity_table"]) as f:
                for r in csv.DictReader(f):
                    self.capacity_table[(int(r["mcs"]), int(r["symbols"]))] = float(
                        r["bytes_per_prb"]
                    )

    def event(self, kind, **data):
        try:
            self.log_queue.put_nowait(
                dict(
                    type=kind,
                    monotonic_ns=time.monotonic_ns(),
                    run_id=self.run_id,
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

    async def exchange(self, m):
        if m.get("schema_version") != 1:
            raise ValueError("schema mismatch")
        run = m["run_id"]
        kind = m["type"]
        if kind == "sync":
            if self.run_id != run:
                self.sessions.clear()
                self.associations = Associations(self.cfg["association"])
                self.run_id = run
            # Reapply complete current state; session identities prevent cross-session increments.
            for a in m.get("associations", []):
                self.associations.register(
                    a["ue_id"],
                    a["connection_id"],
                    a["ip"],
                    a.get("connection_generation"),
                    a.get("association_revision", 0),
                )
            for s in m.get("sessions", []):
                self._session(s)
            self.event("synchronized", sessions=len(self.sessions))
            return {"source_generation": self.source, "status": "synced"}
        if run != self.run_id:
            raise ValueError("sync required for run")
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
                    e = m["applied_expectation"]
                    if (s.get("expectation") or {}).get("config_version") != e[
                        "config_version"
                    ]:
                        s["next_prediction"] = e.get("first_frame_id", fid)
                    s["expectation"] = e
            elif kind == "progress":
                ledger.progress(
                    fid,
                    integer(m["unique_bytes"]),
                    integer(m["progress_seq"]),
                    m.get("complete", False),
                )
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

    def _session(self, m):
        vid = m["video_id"]
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
            )
            self.sessions[vid] = s
        e = m.get("expectation")
        if e and (e.get("applied") or not s.get("expectation")):
            s["expectation"] = e
        s["input_closed"] = m.get("input_closed", False)
        for f in m.get("outstanding", []):
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

    def predict(self, now_ns):
        for s in self.sessions.values():
            e = s.get("expectation")
            if not e or s.get("input_closed") or not e.get("applied"):
                continue
            start = e["first_tx_unix_ns"]
            fps = e["fps"]
            base = e.get("first_frame_id", 0)
            last = base + int((now_ns - start) * fps / 1e9)
            if last < 0:
                continue
            # Bounded catch-up; excessive gaps invalidate summaries rather than cancelling UL traffic.
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
                records = self.trace.poll()
                if self.trace.generation != self.last_trace_generation:
                    self.last_trace_generation = self.trace.generation
                    for uid in list(self.associations.bindings):
                        self.associations.invalidate(uid, "trace_source_generation")
                    self.capacity.clear()
                    self.event(
                        "radio_source_generation", generation=self.last_trace_generation
                    )
                self.associations.tick(
                    now, set(self.metrics) if now - self.latest_metric < 0.3 else set()
                )
                command = self.associations.next_probe(now)
                if command:
                    self.probe_commands.append(command)
                    self.event("probe_command", command=command)
                self.predict(unix)
                if self.window is None:
                    start = unix // self.window_ns * self.window_ns
                    self.window = Window(start, start + self.window_ns)
                for r in records:
                    mapped = (
                        r["time_ns"] + self.trace.mapping
                        if self.trace.mapping is not None
                        else unix
                    )
                    # Attribute to measurement windows, never silently to receive time.
                    while mapped >= self.window.end_ns:
                        self.publish(now)
                        self.window = Window(
                            self.window.end_ns, self.window.end_ns + self.window_ns
                        )
                    if mapped < self.window.start_ns:
                        self.event("late_trace", measurement_unix_ns=mapped)
                    else:
                        self.window.add(r)
                    if (
                        r["kind"] == 3
                        and r["direction"] == 1
                        and r["stage"] == 9
                        and r["prbs"] > 0
                    ):
                        # Reference table generated with actual OAI TBS is preferred. Ratio is an opt-in proxy.
                        cap = self.capacity_table.get((r["mcs"], r["symbols"]))
                        proxy = cap is None
                        if cap is None and self.cfg.get("allow_capacity_proxy"):
                            cap = r["tbs"] / r["prbs"]
                        if cap:
                            self.capacity[r["rnti"]] = (
                                cap,
                                now - max(0, (unix - mapped) / 1e9),
                                proxy,
                            )
                        if (
                            not r["retransmission"]
                            and self.cfg.get("payload_fraction") is not None
                        ):
                            matching = []
                            for s in self.sessions.values():
                                b = self.associations.valid(s["ue_id"], now)
                                if b and b.rnti == r["rnti"]:
                                    matching.extend(s["ledger"].frames.values())
                            left = r["tbs"] * self.cfg["payload_fraction"]
                            for f in sorted(
                                matching, key=lambda x: (x.start_ns, x.frame_id)
                            ):
                                used = min(left, f.remaining)
                                f.estimated += used
                                left -= used
                                if left <= 0:
                                    break
                            if matching:
                                min(
                                    matching, key=lambda x: (x.start_ns, x.frame_id)
                                ).scheduled += r["tbs"]
                while unix >= self.window.end_ns:
                    self.publish(now)
                    self.window = Window(
                        self.window.end_ns, self.window.end_ns + self.window_ns
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

    def publish(self, now):
        record = self.window.record()
        allocated = record.pop("allocated_by_rnti")
        per = {}
        for uid in self.associations.bindings:
            b = self.associations.valid(uid, now)
            r = allocated.get(b.rnti, {}) if b else {}
            capacity = self.capacity.get(b.rnti) if b else None
            rate = r.get("tbs_bytes", 0) / (self.window_ns / 1e9)
            fraction = self.cfg.get("payload_fraction")
            per[uid] = dict(
                association_valid=bool(b),
                connection_id=self.associations.bindings[uid].connection_id,
                mapping_generation=self.associations.bindings[uid].generation,
                scheduled_prbs=r.get("prbs", 0),
                scheduled_tbs_bytes=r.get("tbs_bytes", 0),
                retransmission_prbs=r.get("retry_prbs", 0),
                retransmission_tbs_bytes=r.get("retry_tbs_bytes", 0),
                throughput_bytes_s=rate * fraction if fraction is not None else None,
                throughput_kind="scheduled_payload_conversion_estimate",
                throughput_valid=bool(
                    b
                    and fraction is not None
                    and self.trace.coverage_valid
                    and self.window.events
                ),
                capacity_bytes_per_prb=capacity[0] if capacity else None,
                capacity_kind="realized_ratio_proxy"
                if capacity and capacity[2]
                else "OAI_reference_table",
                capacity_age_s=now - capacity[1] if capacity else None,
            )
        self.sequence += 1
        self.latest_report = dict(
            schema_version=1,
            type="radio_report",
            run_id=self.run_id,
            sequence=self.sequence,
            source_generation=self.source,
            radio_generation=self.trace.generation,
            **record,
            ues=per,
            trace_coverage_valid=self.trace.coverage_valid,
            metric_age_s=now - self.latest_metric,
        )
        self.event("radio_report", report=self.latest_report)
        if self.sequence % 50 == 0:
            data = dict(
                run_id=self.run_id,
                source_generation=self.source,
                bindings={u: asdict(b) for u, b in self.associations.bindings.items()},
                matches=self.associations.last_result,
            )
            asyncio.create_task(asyncio.to_thread(self._snapshot, data))
