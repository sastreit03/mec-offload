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
from .playlist import Playlist


# Map UE monotonic time to MEC wall time; keep SLO durations on the UE clock.
class Clock:
    def __init__(self):
        self.offset = None
        self.uncertainty = None
        self.updated = 0

    # Prefer lower-uncertainty four-timestamp samples, refreshing old mappings.
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
        self.campaign_id = None
        self.scenario_id = None
        self.attempt_id = None
        self.assignment = None
        self.scenario_start_unix_ns = None
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
        self.playlist = Playlist(args.source_dir, args.shuffle_seed) if args.source_dir else None
        self.selection = self.playlist.next() if self.playlist else None
        if self.selection:
            self.args.source = str(self.selection.path)
        self.prepared = None
        self.successor_request = None
        self.successor_selection = None
        self.awaiting_successor = False
        self.successor_prepare_s = 0.0
        self.pending_start = False
        self.start_request = None
        self.transmitted = {}
        self.result_ids = set()
        self.stopped = False
        self.events = asyncio.Queue(maxsize=4096)
        self.loop = None
        self.slo_ns = 150_000_000
        self.collection_mode = False
        self.input_profile = dict(width=1280, height=720, fps=30, gop_frames=30)
        self.expiry_tasks = set()
        self.finishing = False
        self.receipts_db = sqlite3.connect(self.root / "receipts.sqlite3")
        self.receipts_db.execute(
            "CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)"
        )
        if self.playlist:
            self.record(
                "playlist_initialized",
                seed=self.playlist.seed,
                videos=len(self.playlist.files),
            )
            self.record_selection(self.selection)

    def record_selection(self, selection):
        self.record(
            "video_selected",
            source=selection.path.name,
            playlist_cycle=selection.cycle,
            playlist_position=selection.position,
            shuffle_seed=self.playlist.seed,
        )

    def record(self, kind, **data):
        source_context = data.pop("_context", None)
        context = (
            {
                key: source_context.get(key)
                for key in ("campaign_id", "scenario_id", "attempt_id")
            }
            if source_context is not None
            else self.join_context()
        )
        try:
            self.events.put_nowait(
                dict(
                    type=kind,
                    run_id=self.run_id,
                    ue_id=self.uid,
                    **context,
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

    def refresh_server_route(self, source_ip):
        if not self.args.source_interface:
            return
        server_ip = socket.gethostbyname(self.args.server)
        # Aetheredge: a PDU reattach may change the UE address without replacing
        # the interface, so refresh the host route before every control reconnect.
        subprocess.run(
            [
                "ip",
                "route",
                "replace",
                f"{server_ip}/32",
                "dev",
                self.args.source_interface,
                "src",
                source_ip,
            ],
            check=True,
        )

    async def send(self, value):
        if self.ws:
            # Aetheredge: put the immutable scenario join key on every UE message.
            value.update(self.join_context())
            await self.ws.send(json.dumps(value, separators=(",", ":")))

    def join_context(self):
        return dict(
            campaign_id=self.campaign_id,
            scenario_id=self.scenario_id,
            attempt_id=self.attempt_id,
        )

    def set_context(self, message):
        context = {
            key: message.get(key)
            for key in ("campaign_id", "scenario_id", "attempt_id")
        }
        values = tuple(context.values())
        if any(value is not None for value in values) and not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError("incomplete scenario context")
        self.campaign_id = context["campaign_id"]
        self.scenario_id = context["scenario_id"]
        self.attempt_id = context["attempt_id"]

    def context_ready(self):
        return all(self.join_context().values())

    # The media thread posts frame evidence back to the asyncio control loop.
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
        elif kind == "successor_needed" and self.playlist and not self.collection_mode:
            if (
                self.streamer
                and value.get("video_id") == self.streamer.response["video_id"]
                and self.successor_request is None
                and self.prepared is None
            ):
                self.successor_selection = self.playlist.next()
                self.record_selection(self.successor_selection)
                self.successor_request = dict(
                    type="video_start",
                    ue_id=self.uid,
                    client_seq=self.next_seq(),
                    predecessor_video_id=value["video_id"],
                    expected_start_unix_ns=value["expected_start_unix_ns"],
                    application=self.args.application,
                    slo_ms=150,
                    can_drop=False,
                    can_drop_frames=False,
                    input=dict(self.input_profile),
                )
                await self.send(self.successor_request)
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
            self.awaiting_successor = bool(self.playlist) and not self.collection_mode
            if self.prepared:
                self.start_prepared()
            elif self.playlist:
                self.record(
                    "successor_response_pending", reason="response_pending_at_eof"
                )

    def start_prepared(self):
        """Promote a locally prepared response without waiting for old completion."""
        if not self.prepared or not self.successor_selection:
            return
        response = self.prepared
        selection = self.successor_selection
        self.streamer = Streamer(
            response,
            str(selection.path),
            self.args.source_ip,
            self.clock,
            self.media_event,
            self.successor_prepare_s,
        )
        self.selection = selection
        self.args.source = str(selection.path)
        self.prepared = None
        self.successor_request = None
        self.successor_selection = None
        self.awaiting_successor = False
        self.streamer.start()
        self.record("successor_started", video_id=response["video_id"])

    # Record deadline failure independently of result arrival, then reconcile missing data.
    async def expiry(self, key, tx):
        await asyncio.sleep(max(0, (tx + self.slo_ns - time.monotonic_ns()) / 1e9))
        value = self.transmitted.get(key)
        if value and not value.get("received"):
            value["deadline_failed"] = True
            self.record("deadline_miss", video_id=key[0], frame_id=key[1])
        if self.collection_mode:
            # Collection keeps late frames open until their terminal MEC result arrives.
            if value and value.get("received"):
                self.transmitted.pop(key, None)
            return
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
        await asyncio.sleep(0.2 if self.collection_mode else 5.2)
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

        try:
            await asyncio.to_thread(run)
            self.record("association_probe", probe_id=m["probe_id"])
        except OSError as error:
            # MEC restarts can invalidate an in-progress UDP association probe;
            # the control reconnect will request fresh probes after readiness.
            self.record(
                "association_probe_failed",
                probe_id=m.get("probe_id"),
                reason=repr(error),
            )

    # Refresh clock/address evidence; an interface address change forces reconnect.
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
                    ue_alias=self.args.ue_alias,
                    ue_ip=self.args.source_ip,
                    clock_uncertainty_ns=self.clock.uncertainty,
                    clock_mapping=dict(
                        offset_ns=self.clock.offset, method="four_timestamp_min_RTT"
                    ),
                )
            )
            await asyncio.sleep(1)
            await self.send(dict(type="time_sync", client_mono_ns=time.monotonic_ns()))

    async def start_collection_at(self, start_unix_ns, attempt_id):
        """Issue admission at the shared campaign timestamp for this attempt."""
        delay = (start_unix_ns - self.clock.unix_ns()) / 1e9
        if delay > 0:
            await asyncio.sleep(delay)
        if (
            self.stopped
            or self.attempt_id != attempt_id
            or not self.assignment
            or self.streamer is not None
            or self.start_request is not None
        ):
            return
        self.start_request = dict(
            type="video_start",
            ue_id=self.uid,
            client_seq=self.next_seq(),
            application=self.args.application,
            slo_ms=150,
            can_drop=False,
            can_drop_frames=False,
            input=dict(self.input_profile),
        )
        self.start_request.update(
            {
                key: self.assignment[key]
                for key in (
                    "candidate_id",
                    "media_id",
                    "source_start_frame",
                    "frame_count",
                )
            }
        )
        self.record(
            "scenario_start",
            scheduled_start_unix_ns=start_unix_ns,
            actual_request_unix_ns=self.clock.unix_ns(),
        )
        await self.send(self.start_request)

    # Reconnect with the saved identity while the media session can continue separately.
    async def run(self):
        import websockets

        self.loop = asyncio.get_running_loop()
        writer = asyncio.create_task(self.writer())
        try:
            while not self.stopped:
                try:
                    self.args.source_ip = await asyncio.to_thread(self.source_ip)
                    await asyncio.to_thread(
                        self.refresh_server_route, self.args.source_ip
                    )
                    if self.streamer:
                        self.streamer.source_ip = self.args.source_ip
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.bind((self.args.source_ip, 0))
                    sock.setblocking(False)
                    try:
                        # Bound failed data-plane attempts so route/address changes
                        # are detected promptly by the next reconnect iteration.
                        await asyncio.wait_for(
                            self.loop.sock_connect(
                                sock, (self.args.server, self.args.port)
                            ),
                            timeout=5,
                        )
                    except Exception:
                        sock.close()
                        raise
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
                                    self.set_context(m)
                                    self.connection = m["connection_id"]
                                    # Request the validated media profile advertised by this MEC.
                                    self.input_profile = m.get(
                                        "input_profile", self.input_profile
                                    )
                                    self.successor_prepare_s = float(
                                        m.get("successor_prepare_s", 5.0)
                                    )
                                    self.collection_mode = bool(
                                        m.get("collection_mode", False)
                                    )
                                    self.state["resume_token"] = m["resume_token"]
                                    self.save_state()
                                    sync = asyncio.create_task(self.synchronize())
                                elif kind == "scenario_context":
                                    if self.streamer or self.prepared:
                                        raise RuntimeError(
                                            "scenario context changed during active media"
                                        )
                                    self.set_context(m)
                                    self.assignment = (
                                        m.get("assignment") if m.get("active") else None
                                    )
                                    self.scenario_start_unix_ns = None
                                    if self.assignment:
                                        self.args.application = self.assignment["application"]
                                        self.args.source = self.assignment["media_path"]
                                    self.start_request = None
                                    self.successor_request = None
                                    self.record("scenario_context")
                                    # Confirm that this control generation received
                                    # the exact assignment before the runner trusts it.
                                    await self.send(dict(type="scenario_context_ack"))
                                elif kind == "scenario_start":
                                    self.scenario_start_unix_ns = int(m["start_unix_ns"])
                                    asyncio.create_task(
                                        self.start_collection_at(
                                            self.scenario_start_unix_ns,
                                            self.attempt_id,
                                        )
                                    )
                                elif kind == "time_sync":
                                    self.clock.observe(
                                        m["client_mono_ns"],
                                        m["server_receive_unix_ns"],
                                        m["server_send_unix_ns"],
                                        time.monotonic_ns(),
                                    )
                                elif kind == "association_probe":
                                    asyncio.create_task(self.probe(m))
                                elif kind == "association_status" and m["valid"]:
                                    if self.collection_mode and not self.context_ready():
                                        continue
                                    if self.collection_mode and not self.assignment:
                                        continue
                                    if self.collection_mode:
                                        # Collection starts only after the runner's
                                        # all-UE timestamp barrier is released.
                                        continue
                                    if self.successor_request is not None:
                                        # Deduplicated resend covers a control reconnect during preparation.
                                        await self.send(self.successor_request)
                                    elif self.streamer is None:
                                        if self.start_request is None:
                                            self.start_request = dict(
                                                type="video_start",
                                                ue_id=self.uid,
                                                client_seq=self.next_seq(),
                                                application=self.args.application,
                                                slo_ms=150,
                                                can_drop=False,
                                                can_drop_frames=False,
                                                input=dict(self.input_profile),
                                            )
                                            if self.assignment:
                                                self.start_request.update(
                                                    {
                                                        key: self.assignment[key]
                                                        for key in (
                                                            "candidate_id",
                                                            "media_id",
                                                            "source_start_frame",
                                                            "frame_count",
                                                        )
                                                    }
                                                )
                                        await self.send(self.start_request)
                                elif kind == "video_response":
                                    if (
                                        self.successor_request
                                        and m.get("client_seq")
                                        == self.successor_request["client_seq"]
                                    ):
                                        if m["accepted"]:
                                            self.record(
                                                "admission_decision",
                                                video_id=m["video_id"],
                                                accepted=True,
                                                reason=None,
                                            )
                                            self.prepared = m
                                            if self.awaiting_successor:
                                                self.start_prepared()
                                        else:
                                            self.record(
                                                "admission_decision",
                                                video_id=m.get("video_id"),
                                                accepted=False,
                                                reason=m.get("reason"),
                                            )
                                            self.record(
                                                "successor_not_prepared",
                                                reason=m.get("reason"),
                                            )
                                            self.successor_request = None
                                    elif m["accepted"] and self.streamer is None:
                                        self.record(
                                            "admission_decision",
                                            video_id=m["video_id"],
                                            accepted=True,
                                            reason=None,
                                        )
                                        self.slo_ns = round(m["slo_ms"] * 1e6)
                                        self.streamer = Streamer(
                                            m,
                                            self.args.source,
                                            self.args.source_ip,
                                            self.clock,
                                            self.media_event,
                                            self.successor_prepare_s
                                            if self.playlist and not self.collection_mode
                                            else 0,
                                            source_start_frame=(
                                                self.assignment or {}
                                            ).get("source_start_frame", 0),
                                            frame_count=(self.assignment or {}).get(
                                                "frame_count"
                                            ),
                                        )
                                        if self.successor_selection:
                                            self.selection = self.successor_selection
                                            self.successor_selection = None
                                            self.awaiting_successor = False
                                        self.streamer.start()
                                    elif not m["accepted"]:
                                        self.record(
                                            "admission_decision",
                                            video_id=m.get("video_id"),
                                            accepted=False,
                                            reason=m.get("reason"),
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
                                elif (
                                    kind == "video_status" and self.streamer
                                    and m["video_id"] == self.streamer.response["video_id"]
                                ):
                                    if m["status"] == "SUSPENDED":
                                        self.streamer.suspend(m)
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
                                        finished_streamer = self.streamer
                                        finished_streamer.stop()
                                        if self.collection_mode:
                                            self.streamer = None
                                            self.start_request = None
                                            # Aetheredge: wait for the next attempt's
                                            # scenario_context instead of restarting.
                                            self.assignment = None
                                            self.scenario_start_unix_ns = None
                                            await asyncio.to_thread(
                                                finished_streamer.thread.join, 3
                                            )
                                        elif not self.playlist:
                                            asyncio.create_task(self.finish())
                                        else:
                                            # Admission failure falls back to the normal post-terminal path.
                                            self.streamer = None
                                            self.start_request = None
                                            if self.successor_selection:
                                                self.args.source = str(
                                                    self.successor_selection.path
                                                )
                                elif (
                                    kind == "video_status"
                                    and self.prepared
                                    and m["video_id"] == self.prepared["video_id"]
                                    and m["status"] in ("FAILED", "EXPIRED", "CANCELLED")
                                ):
                                    self.record(
                                        "successor_not_prepared", reason=m["status"]
                                    )
                                    self.prepared = None
                                    self.successor_request = None
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
                                        _context=m,
                                        video_id=m["video_id"],
                                        frame_id=m["frame_id"],
                                        result_id=m["result_id"],
                                        outcome=m["outcome"],
                                        reason=m.get("reason"),
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
                                    if self.collection_mode:
                                        self.transmitted.pop(key, None)
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
    p.add_argument("--source-dir", help="directory of prerecorded videos to shuffle")
    p.add_argument("--shuffle-seed", type=int, help="reproducible playlist seed")
    p.add_argument(
        "--ue-alias",
        default="linux1",
        help="stable campaign identity assigned in the manifest",
    )
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
