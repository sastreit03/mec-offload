"""Read existing scheduler trace v2. Scheduled TBS is explicitly not unique delivery."""

import csv
import io
import os
import time
import glob
from collections import defaultdict


class TraceReader:
    def __init__(self, path):
        self.path = str(path)
        self.file = None
        self.header = None
        self.identity = None
        self.generation = 0
        self.coverage_valid = False
        self.last_dropped = 0
        self.mapping = None

    # Follow trace rotation and preserve measurement clocks when consuming late rows.
    def poll(self):
        candidates = glob.glob(self.path)
        if not candidates:
            return []
        resolved = max(candidates, key=os.path.getmtime)
        try:
            stat = os.stat(resolved)
        except FileNotFoundError:
            return []
        ident = (stat.st_dev, stat.st_ino)
        if (
            self.file is None
            or ident != self.identity
            or stat.st_size < self.file.tell()
        ):
            if self.file:
                self.file.close()
            self.file = open(resolved)
            self.identity = ident
            self.header = None
            self.generation += 1
        records = []
        for _ in range(20000):
            pos = self.file.tell()
            line = self.file.readline()
            if not line:
                break
            if not line.endswith("\n"):
                self.file.seek(pos)
                break
            if line.startswith("#"):
                self.generation += 1
                try:
                    fields = dict(s.strip().split("=", 1) for s in line.split(";")[1:])
                    self.mapping = int(fields["realtime_ns"]) - int(
                        fields["monotonic_ns"]
                    )
                except (ValueError, KeyError):
                    self.mapping = None
                continue
            values = next(csv.reader([line]))
            if values[0] == "kind":
                self.header = values
                continue
            if not self.header or len(values) != len(self.header):
                self.coverage_valid = False
                continue
            try:
                r = {
                    k: float(v) if k == "weight" else int(v)
                    for k, v in zip(self.header, values)
                }
            except ValueError:
                self.coverage_valid = False
                continue
            self.coverage_valid = (
                r["dropped"] == self.last_dropped and r["io_errors"] == 0
            )
            self.last_dropped = r["dropped"]
            records.append(r)
        return records


class Window:
    def __init__(self, start_ns, end_ns, source="scheduler_trace_v2"):
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.source = source
        self.ues = defaultdict(
            lambda: dict(
                prbs=0,
                tbs_bytes=0,
                retry_prbs=0,
                retry_tbs_bytes=0,
                dl_prbs=0,
                dl_tbs_bytes=0,
                dl_retry_prbs=0,
                dl_retry_tbs_bytes=0,
            )
        )
        self.opportunities = {}
        self.events = 0

    # Separate retry resource costs from new-data scheduled capacity.
    def add(self, r):
        if r["kind"] != 3 or r["direction"] != 1:
            return
        self.events += 1
        if r["reference"] >= 0:
            key = (r["target_frame"], r["target_slot"], r["beam"])
            self.opportunities[key] = max(
                self.opportunities.get(key, 0), r["reference"]
            )
        if r["stage"] != 9:
            return
        u = self.ues[r["rnti"]]
        retry = bool(r["retransmission"])
        u["retry_prbs" if retry else "prbs"] += r["prbs"]
        u["retry_tbs_bytes" if retry else "tbs_bytes"] += r["tbs"]

    # Aetheredge: Aggregate loss-proof ZMQ counter deltas without CSV parsing.
    def add_counters(
        self,
        rnti,
        prbs,
        tbs_bytes,
        retry_prbs,
        retry_tbs_bytes,
        dl_prbs=0,
        dl_tbs_bytes=0,
        dl_retry_prbs=0,
        dl_retry_tbs_bytes=0,
    ):
        self.events += 1
        u = self.ues[rnti]
        u["prbs"] += prbs
        u["tbs_bytes"] += tbs_bytes
        u["retry_prbs"] += retry_prbs
        u["retry_tbs_bytes"] += retry_tbs_bytes
        u["dl_prbs"] += dl_prbs
        u["dl_tbs_bytes"] += dl_tbs_bytes
        u["dl_retry_prbs"] += dl_retry_prbs
        u["dl_retry_tbs_bytes"] += dl_retry_tbs_bytes

    def record(self):
        return dict(
            window_start_unix_ns=self.start_ns,
            window_end_unix_ns=self.end_ns,
            observed_residual_prb_opportunities=sum(self.opportunities.values()),
            total_ul_opportunity_valid=False,
            source=self.source,
            allocated_by_rnti=dict(self.ues),
            trace_events=self.events,
        )
