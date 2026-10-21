from __future__ import annotations
import argparse
import asyncio
import json
import socket
import time
import sqlite3
import subprocess
from pathlib import Path
from .media import Streamer


class Clock:
    def __init__(self):
        self.offset = None
        self.uncertainty = None
        self.updated = 0

    def observe(self, t0, t1, t2, t3):
        uncertainty = max(0, ((t3 - t0) - (t2 - t1)) // 2)
        if (
            self.uncertainty is None
            or uncertainty < self.uncertainty
            or time.monotonic() - self.updated > 30
        ):
            self.offset = ((t1 - t0) + (t2 - t3)) // 2
            self.uncertainty = uncertainty
            self.updated = time.monotonic()

    def valid(self):
        return self.offset is not None and time.monotonic() - self.updated < 60

    def to_unix(self, mono):
        return mono + self.offset

    def unix_ns(self):
        return self.to_unix(time.monotonic_ns()) if self.valid() else time.time_ns()


class Client:
    def __init__(self, args):
        self.args = args
        self.clock = Clock()
        self.ws = None
        self.uid = None
        self.run_id = None
        self.connection = None
        self.root = Path(args.logs)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "identity.json"
        self.state = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else dict(client_seq=0)
        )
        self.streamer = None
        self.pending_start = False
        self.start_request = None
        self.transmitted = {}
        self.result_ids = set()
        self.stopped = False
        self.events = asyncio.Queue(maxsize=4096)
        self.loop = None
        self.slo_ns = 150_000_000
        self.expiry_tasks = set()
        self.finishing = False
        self.receipts_db = sqlite3.connect(self.root / "receipts.sqlite3")
        self.receipts_db.execute(
            "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)"
        )

    def record(self, kind, **data):
        try:
            self.events.put_nowait(
                dict(
                    type=kind,
                    run_id=self.run_id,
                    ue_id=self.uid,
                    monotonic_ns=time.monotonic_ns(),
                    **data,
                )
            )
        except asyncio.QueueFull:
            raise RuntimeError("UE evidence queue full")

    async def writer(self):
        while True:
            item = await self.events.get()
            try:
                await asyncio.to_thread(self.append, item)
            finally:
                self.events.task_done()

    def append(self, item):
        with (self.root / "events.jsonl").open("a") as f:
            f.write(json.dumps(item, allow_nan=False) + "\n")

    def save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_path)

    def next_seq(self):
        self.state["client_seq"] += 1
        self.save_state()
        return self.state["client_seq"]

    def source_ip(self):
        if self.args.source_interface:
            rows = json.loads(
                subprocess.check_output(
                    [
                        "ip",
                        "-j",
                        "-4",
                        "addr",
                        "show",
                        "dev",
                        self.args.source_interface,
                    ],
                    text=True,
                )
            )
            return next(
                x["local"]
                for r in rows
                for x in r["addr_info"]
                if x.get("family") == "inet"
            )
        return self.args.source_ip

    async def send(self, value):
        if self.ws:
            await self.ws.send(json.dumps(value, separators=(",", ":")))

    def media_event(self, kind, value):
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.on_media(kind, value))
        )

    async def on_media(self, kind, value):
        self.record(kind, **value)
        if kind == "transmitted":
            key = (value["video_id"], value["frame_id"])
            self.transmitted[key] = value
            task = asyncio.create_task(self.expiry(key, value["tx_mono_ns"]))
            self.expiry_tasks.add(task)
            task.add_done_callback(self.expiry_tasks.discard)
        elif (
            kind == "media_end"
            and self.streamer
            and value.get("video_id") == self.streamer.response["video_id"]
            and value["final_frame_id"] >= 0
        ):
            await self.send(
                dict(
                    type="video_end",
                    ue_id=self.uid,
                    video_id=self.streamer.response["video_id"],
                    client_seq=self.next_seq(),
                    final_frame_id=value["final_frame_id"],
                )
            )

    async def expiry(self, key, tx):
        await asyncio.sleep(max(0, (tx + self.slo_ns - time.monotonic_ns()) / 1e9))
        value = self.transmitted.get(key)
        if value and not value.get("received"):
            value["deadline_failed"] = True
            self.record("deadline_miss", video_id=key[0], frame_id=key[1])
        await asyncio.sleep(5)
        value = self.transmitted.get(key)
        if value and not value.get("received"):
            self.record("final_missing", video_id=key[0], frame_id=key[1])
            value["final_missing"] = True
        self.transmitted.pop(key, None)

    async def finish(self):
        if self.finishing:
            return
        self.finishing = True
        await asyncio.sleep(5.2)
        self.stopped = True
        if self.ws:
            await self.ws.close()

    async def probe(self, m):
        def run():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind((self.args.source_ip, 0))
                s.connect((m["endpoint"]["host"], m["endpoint"]["port"]))
                for i, on in enumerate(m["pattern"]):
                    begin = m["start_unix_ns"] + round(i * m["interval_s"] * 1e9)
                    end = begin + round(m["interval_s"] * 1e9)
                    while self.clock.unix_ns() < begin:
                        time.sleep(min(0.01, (begin - self.clock.unix_ns()) / 1e9))
                    header = f"MECP1 {self.uid} {self.connection} {m['probe_id']} {i}\n".encode()
                    packet = header + b"0" * max(0, 1000 - len(header))
                    next_send = self.clock.unix_ns()
                    while on and self.clock.unix_ns() < end:
                        s.send(packet)
                        next_send += round(len(packet) * 8 / m["rate_bps"] * 1e9)
                        delay = (next_send - self.clock.unix_ns()) / 1e9
                        if delay > 0:
                            time.sleep(delay)

        await asyncio.to_thread(run)
        self.record("association_probe", probe_id=m["probe_id"])

    async def synchronize(self):
        for _ in range(4):
            await self.send(dict(type="time_sync", client_mono_ns=time.monotonic_ns()))
            await asyncio.sleep(0.05)
        while self.ws:
            try:
                current = await asyncio.to_thread(self.source_ip)
            except Exception:
                await self.ws.close()
                return
            if current != self.args.source_ip:
                self.args.source_ip = current
                if self.streamer:
                    self.streamer.source_ip = current
                await self.ws.close()
                return
            await self.send(
                dict(
                    type="association_update",
                    ue_ip=self.args.source_ip,
                    clock_uncertainty_ns=self.clock.uncertainty,
                    clock_mapping=dict(
                        offset_ns=self.clock.offset, method="four_timestamp_min_RTT"
                    ),
                )
            )
            await asyncio.sleep(1)
            await self.send(dict(type="time_sync", client_mono_ns=time.monotonic_ns()))

    async def run(self):
        import websockets

        self.loop = asyncio.get_running_loop()
        writer = asyncio.create_task(self.writer())
        try:
            while not self.stopped:
                try:
                    self.args.source_ip = await asyncio.to_thread(self.source_ip)
                    if self.streamer:
                        self.streamer.source_ip = self.args.source_ip
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.bind((self.args.source_ip, 0))
                    sock.setblocking(False)
                    await self.loop.sock_connect(
                        sock, (self.args.server, self.args.port)
                    )
                    headers = (
                        {"x-mec-resume-token": self.state["resume_token"]}
                        if self.state.get("resume_token")
                        else {}
                    )
                    async with websockets.connect(
                        f"ws://{self.args.server}:{self.args.port}/ws/ue",
                        sock=sock,
                        additional_headers=headers,
                        max_size=None,
                        ping_interval=10,
                    ) as ws:
                        self.ws = ws
                        sync = None
                        try:
                            async for raw in ws:
                                m = json.loads(raw)
                                kind = m.get("type")
                                if kind == "mec_hello":
                                    if self.streamer and (
                                        m.get("active_video_id")
                                        != self.streamer.response["video_id"]
                                        or self.run_id
                                        and m["run_id"] != self.run_id
                                    ):
                                        if self.streamer:
                                            self.streamer.stop()
                                            self.streamer = None
                                        self.start_request = None
                                    self.uid = m["ue_id"]
                                    self.run_id = m["run_id"]
                                    self.connection = m["connection_id"]
                                    self.state["resume_token"] = m["resume_token"]
                                    self.save_state()
                                    sync = asyncio.create_task(self.synchronize())
                                elif kind == "time_sync":
                                    self.clock.observe(
                                        m["client_mono_ns"],
                                        m["server_receive_unix_ns"],
                                        m["server_send_unix_ns"],
                                        time.monotonic_ns(),
                                    )
                                elif kind == "association_probe":
                                    asyncio.create_task(self.probe(m))
                                elif (
                                    kind == "association_status"
                                    and m["valid"]
                                    and self.streamer is None
                                ):
                                    if self.start_request is None:
                                        self.start_request = dict(
                                            type="video_start",
                                            ue_id=self.uid,
                                            client_seq=self.next_seq(),
                                            application=self.args.application,
                                            slo_ms=150,
                                            can_drop=False,
                                            can_drop_frames=False,
                                            input=dict(width=1280, height=720, fps=30),
                                        )
                                    await self.send(self.start_request)
                                elif kind == "video_response":
                                    if m["accepted"] and self.streamer is None:
                                        self.slo_ns = round(m["slo_ms"] * 1e6)
                                        self.streamer = Streamer(
                                            m,
                                            self.args.source,
                                            self.args.source_ip,
                                            self.clock,
                                            self.media_event,
                                        )
                                        self.streamer.start()
                                    elif not m["accepted"]:
                                        self.record(
                                            "admission_denied", reason=m.get("reason")
                                        )
                                        self.start_request = None
                                        await asyncio.sleep(0.5)
                                elif (
                                    kind == "gop_config"
                                    and self.streamer
                                    and m["video_id"]
                                    == self.streamer.response["video_id"]
                                ):
                                    self.streamer.command(m)
                                elif kind == "video_status" and self.streamer:
                                    if m["status"] == "SUSPENDED":
                                        self.streamer.pause_at = m["effective_unix_ns"]
                                    elif m["status"] == "EXPIRED":
                                        self.streamer.stop()
                                        self.streamer = None
                                        self.start_request = None
                                    elif m["status"] in (
                                        "COMPLETED",
                                        "FAILED",
                                        "EXPIRED",
                                        "CANCELLED",
                                    ):
                                        self.streamer.stop()
                                        asyncio.create_task(self.finish())
                                elif kind == "frame_result":
                                    if self.receipts_db.execute(
                                        "SELECT 1 FROM seen WHERE id=?",
                                        (m["result_id"],),
                                    ).fetchone():
                                        continue
                                    self.receipts_db.execute(
                                        "INSERT INTO seen VALUES (?)", (m["result_id"],)
                                    )
                                    self.receipts_db.commit()
                                    key = (m["video_id"], m["frame_id"])
                                    tx = self.transmitted.get(key)
                                    latency = (
                                        time.monotonic_ns() - tx["tx_mono_ns"]
                                        if tx
                                        else None
                                    )
                                    if tx:
                                        tx["received"] = True
                                    self.record(
                                        "result_receipt",
                                        video_id=m["video_id"],
                                        frame_id=m["frame_id"],
                                        result_id=m["result_id"],
                                        outcome=m["outcome"],
                                        latency_ns=latency,
                                        receipt_mono_ns=time.monotonic_ns(),
                                        slo_success=bool(
                                            latency is not None
                                            and latency <= self.slo_ns
                                            and m["outcome"] == "completed"
                                        ),
                                        late_amendment=bool(
                                            tx and tx.get("final_missing")
                                        ),
                                    )
                                    print(
                                        json.dumps(
                                            {
                                                k: v
                                                for k, v in m.items()
                                                if k != "payload"
                                            }
                                        ),
                                        flush=True,
                                    )
                                elif kind == "error":
                                    self.record("server_error", reason=m.get("reason"))
                                if self.stopped:
                                    break
                        finally:
                            if sync:
                                sync.cancel()
                                await asyncio.gather(sync, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    self.record("control_reconnect", reason=repr(ex))
                    self.ws = None
                    await asyncio.sleep(0.5)
        finally:
            if self.streamer:
                self.streamer.stop()
            if self.streamer:
                await asyncio.to_thread(self.streamer.thread.join, 3)
            await asyncio.sleep(0)
            if self.expiry_tasks:
                await asyncio.gather(*list(self.expiry_tasks), return_exceptions=True)
            await self.events.join()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--source-ip")
    p.add_argument(
        "--source-interface",
        help="derive and refresh softUE IPv4 address automatically",
    )
    p.add_argument("--source", default="0", help="camera index or dataset video path")
    p.add_argument(
        "--application",
        choices=["tracking", "detection", "segmentation"],
        default="detection",
    )
    p.add_argument("--logs", default="ue-run")
    args = p.parse_args()
    if not args.source_ip and not args.source_interface:
        p.error("use --source-interface (preferred) or --source-ip")
    asyncio.run(Client(args).run())


if __name__ == "__main__":
    main()
