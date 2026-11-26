"""Linux campaign protocol with the Pixel's pre-encoded sender and receipt instrumentation."""
import asyncio
import json
import re
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mec_ue.client import Client
from playback import Playback

SPACE = re.compile(r'\s*')
JOIN = ("campaign_id", "scenario_id", "attempt_id")
IDS = (*JOIN, "video_id", "frame_id", "result_id")


def envelope(raw):
    """Decode top-level fields only. Older payload-first replay falls back to full JSON."""
    decoder = json.JSONDecoder()
    i = SPACE.match(raw).end()
    if raw[i:i+1] != "{":
        raise ValueError("Expected a JSON object")
    out = {}
    i += 1
    while True:
        i = SPACE.match(raw, i).end()
        if raw[i:i+1] == "}":
            return out
        name, i = decoder.raw_decode(raw, i)
        i = SPACE.match(raw, i).end()
        if raw[i:i+1] != ":":
            raise ValueError("Invalid JSON member")
        if name == "payload" and out.get("type") == "frame_result" and all(k in out for k in (*IDS, "outcome", "reason")):
            return out
        out[name], i = decoder.raw_decode(raw, SPACE.match(raw, i+1).end())
        i = SPACE.match(raw, i).end()
        if raw[i:i+1] == "}":
            return out
        if raw[i:i+1] != ",":
            raise ValueError("Invalid JSON separator")
        i += 1


class Handset(Client):
    def __init__(self, args):
        super().__init__(args)
        self.receipts_db.close()
        self.worker = ThreadPoolExecutor(1, thread_name_prefix="ue-results")
        self.db = None
        self.tx = {}
        self.tx_lock = threading.Lock()
        self.generation = 0
        self.health = dict(probe="Waiting for probe", association="Waiting for report",
                           rtp="Waiting for assignment", sent=0, results=0, error="")
        self.sample_context = None

    def source_ip(self):
        if self.args.bind_ip:
            return self.args.bind_ip
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((self.args.server, self.args.port))
            return sock.getsockname()[0]

    def register(self, video, frame, tx):
        with self.tx_lock:
            self.tx[video, frame] = tx
            if len(self.tx) > 4096:
                self.tx.pop(next(iter(self.tx)))

    def make_streamer(self, response, source, source_ip, clock, event, *args, **kwargs):
        context = self.join_context()

        def media_event(kind, value):
            value["_context"] = context
            event(kind, value)

        return Playback(response, source, source_ip, clock, media_event, *args, **kwargs,
                        folder=Path(self.args.folder), assignment=dict(self.assignment),
                        register=self.register)

    def record(self, kind, **data):
        super().record(kind, **data)
        if kind == "transmitted":
            self.health["sent"] += 1
            if self.health["rtp"] != "Completed result received":
                self.health["rtp"] = "UDP submitted"
        elif kind == "media_prepared":
            self.health["rtp"] = "UDP ready · asset verified"
        elif kind == "association_probe":
            self.health["probe"] = "Sent · " + str(data["probe_id"])
        elif kind == "control_reconnect":
            self.health.update(error=data["reason"], association="Waiting for report", probe="Waiting for probe", rtp="Waiting for evidence")
        elif kind in {"association_probe_failed", "server_error", "sender_error"}:
            self.health["error"] = str(data.get("reason"))
            if kind == "association_probe_failed":
                self.health["probe"] = "Failed"
        elif kind == "scenario_start":
            self.sample_context = self.join_context()
            self.sample(self.sample_context)
        elif kind == "scenario_aborted":
            self.end_samples()
        elif kind == "scenario_context":
            self.health.update(sent=0, results=0, rtp="Waiting for preparation")

    async def writer(self):
        # Flush each available batch on a filesystem thread and drain on clean shutdown.
        def append(f, rows):
            f.writelines(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n" for row in rows)
            f.flush()

        with (self.root / "events.jsonl").open("a") as f:
            while True:
                rows = [await self.events.get()]
                await asyncio.sleep(0.025)
                while len(rows) < 256 and not self.events.empty():
                    rows.append(self.events.get_nowait())
                try:
                    await asyncio.to_thread(append, f, rows)
                except OSError as e:
                    self.health["error"] = f"Evidence write failed: {e}"
                    self.fatal_media_error = self.health["error"]
                    self.stopped = True
                    if self.ws:
                        await self.ws.close()
                finally:
                    for _ in rows:
                        self.events.task_done()

    def decode(self, raw, receipt, ahead):
        start = time.monotonic_ns()
        m = envelope(raw)
        t = dict(receipt_mono_ns=receipt, dispatch_start_mono_ns=start, worker_queue_ns=start-receipt,
                 queued_ahead=ahead, parse_ns=time.monotonic_ns()-start, message_type=m["type"])
        if m["type"] == "frame_result":
            if self.db is None:
                self.db = sqlite3.connect(self.root / "receipts.sqlite3")
            start = time.monotonic_ns()
            t["duplicate"] = bool(self.db.execute("SELECT 1 FROM seen WHERE id=?", (m["result_id"],)).fetchone())
            t["sqlite_select_ns"] = time.monotonic_ns()-start
            if not t["duplicate"]:
                start = time.monotonic_ns()
                with self.db:
                    self.db.execute("INSERT INTO seen VALUES (?)", (m["result_id"],))
                t["sqlite_insert_ns"] = time.monotonic_ns()-start
        return m, t

    async def messages(self, ws):
        self.generation += 1
        self.connection = None
        queue = asyncio.Queue(maxsize=32)
        async def read():
            try:
                async for raw in ws:
                    receipt = time.monotonic_ns()
                    self.last_control_at = time.monotonic()
                    await queue.put((raw, receipt, queue.qsize()))
            except Exception as exc:
                await queue.put(exc)
            else:
                await queue.put(None)
        reader = asyncio.create_task(read())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                raw, receipt, ahead = item
                m, timing = await self.loop.run_in_executor(self.worker, self.decode, raw, receipt, ahead)
                kind = m["type"]
                ctx = m if all(m.get(k) for k in JOIN) else self.join_context()
                timing.update({k:m[k] for k in IDS if k in m and k not in JOIN})
                self.record("incoming_message_timing", _context=ctx, connection_generation=self.generation, **timing)
                if kind == "frame_result":
                    if not timing["duplicate"]:
                        key = (m["video_id"], m["frame_id"])
                        with self.tx_lock:
                            tx = self.tx.pop(key, None)
                        self.transmitted.pop(key, None)
                        latency = receipt-tx if tx is not None else None
                        self.record("result_receipt", _context=m, **{k:m[k] for k in ("video_id","frame_id","result_id","outcome")},
                                    reason=m.get("reason"), receipt_mono_ns=receipt, latency_ns=latency,
                                    slo_success=latency is not None and 0 <= latency <= self.slo_ns and m["outcome"]=="completed")
                        if tx is not None and self.streamer and self.streamer.response["video_id"] == key[0]:
                            self.health["results"] += 1
                            if m["outcome"] == "completed":
                                self.health["rtp"] = "Completed result received"
                    continue
                if kind == "mec_hello":
                    self.health.update(error="", association="Waiting for report", probe="Waiting for probe")
                    if not m.get("collection_mode"):
                        self.health["error"] = "This staged-media client requires MEC collection mode."
                        self.stopped = True
                        return
                elif kind == "association_status":
                    self.health["association"] = "Validated" if m.get("valid") is True else "Unvalidated"
                    continue
                elif kind == "association_probe":
                    self.health["probe"] = "Scheduled · " + str(m["probe_id"])
                elif kind == "video_status" and self.streamer and m.get("video_id") == self.streamer.response["video_id"] and m.get("status") in {"COMPLETED","FAILED","EXPIRED","CANCELLED"}:
                    self.end_samples()
                elif kind == "time_sync":
                    m["_receipt_ns"] = receipt
                yield m
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def on_media(self, kind, value):
        ctx = value.get("_context")
        if ctx and (ctx != self.join_context() or ctx.get("attempt_id") == self.aborted_attempt_id):
            self.record(kind, **value)
            return
        if kind == "plan_unapplied":
            await self.send(dict(type=kind, command_id=value["command_id"], reason=value["reason"]))
        if kind == "transmitted":
            key = (value["video_id"], value["frame_id"])
            with self.tx_lock:
                already_received = key not in self.tx
            if already_received:
                self.record(kind, **value)
                return
        await super().on_media(kind, value)
        if len(self.transmitted) > 4096:
            self.transmitted.pop(next(iter(self.transmitted)))

    async def expiry(self, key, tx):
        value = self.transmitted.get(key)
        if value is None:
            return
        await asyncio.sleep(max(0, (tx + self.slo_ns - time.monotonic_ns()) / 1e9))
        if self.transmitted.get(key) is value:
            self.record("deadline_miss", _context=value["_context"], video_id=key[0], frame_id=key[1])

    def sample(self, ctx):
        self.record("cpu_sample", _context=ctx, sample_mono_ns=time.monotonic_ns(),
                    process_cpu_ms=time.process_time_ns()//1_000_000)

    def end_samples(self):
        if self.sample_context:
            self.sample(self.sample_context)
            self.sample_context = None

    async def sample_cpu(self):
        while True:
            await asyncio.sleep(.25)
            if self.sample_context:
                self.sample(self.sample_context)

    async def run(self):
        timer = asyncio.create_task(self.sample_cpu())
        try:
            await super().run()
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            if self.db is not None:
                await asyncio.get_running_loop().run_in_executor(self.worker, self.db.close)
            self.worker.shutdown()
