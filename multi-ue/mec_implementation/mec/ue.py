"""Matching UE client: identity persistence, request retry, RTP data and UE logs."""
import argparse
import asyncio
import io
import json
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from PIL import Image
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .rtp import Header, packetize


class UEClient:
    def __init__(self, url, ip_address=None, state_path="ue_state.json", log_path="ue_events.jsonl"):
        self.url, self.ip = url, ip_address
        self.state_path = Path(state_path)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "ue_id": None, "resume_token": None, "next_client_seq": 0, "task_ids": [], "seen_result_ids": []}
        self.seen = set(self.state["seen_result_ids"])
        self.events = Path(log_path).open("a")
        self.connected = asyncio.Event()
        self.socket = None
        self.pending = {}
        self.requests = {}
        self.request_start_ns = {}
        self.terminals = {}
        self.terminal_waiters = {}
        self.capture_times = {}
        self.slos = {}
        self.received = asyncio.Queue()
        self.connection_task = None

    def event(self, name, **fields):
        self.events.write(json.dumps({"event": name, "unix_ns": time.time_ns(),
                                      "mono_ns": time.monotonic_ns(), **fields}) + "\n")
        self.events.flush()

    def save(self):
        self.state["seen_result_ids"] = list(self.seen)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    async def start(self):
        self.connection_task = asyncio.create_task(self._connections())
        await asyncio.wait_for(self.connected.wait(), 10)

    async def _connections(self):
        while True:
            headers = {}
            if self.state["resume_token"]:
                headers["X-MEC-Resume-Token"] = self.state["resume_token"]
            try:
                async with connect(self.url, additional_headers=headers, max_size=None,
                                   compression=None, proxy=None) as ws:
                    hello = json.loads(await ws.recv())
                    if hello["type"] != "mec_hello":
                        raise ValueError("Expected mec_hello")
                    previous = self.state["ue_id"]
                    self.state.update(ue_id=hello["ue_id"], resume_token=hello["resume_token"])
                    self.save()
                    if previous and previous != hello["ue_id"]:
                        # Restart is a new run, not runtime recovery. Do not revive
                        # old task IDs or silently create replacements for streams.
                        for future in self.pending.values():
                            if not future.done():
                                future.set_exception(RuntimeError("MEC identity reset; submit a new task"))
                        self.pending.clear()
                        self.requests.clear()
                        for task_id, waiter in self.terminal_waiters.items():
                            if not waiter.done():
                                waiter.set_result({"type": "task_failed", "task_id": task_id, "reason": "server_restart"})
                    self.socket = ws
                    self.event("connection", ue_id=hello["ue_id"], connection_id=hello["connection_id"], run_id=hello["run_id"])
                    self.connected.set()
                    for request in list(self.requests.values()):
                        if request["client_seq"] in self.pending:
                            await ws.send(json.dumps(request))
                            self.event("request_retry", client_seq=request["client_seq"])
                    async for raw in ws:
                        self._receive(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionClosed, ValueError) as exc:
                self.event("connection_lost", reason=str(exc))
            finally:
                self.connected.clear()
                self.socket = None
            await asyncio.sleep(0.5)

    def _receive(self, message):
        typ = message.get("type")
        if "result_id" in message:
            if message["result_id"] in self.seen:
                return
            self.seen.add(message["result_id"])
            self.save()
        task_id = message.get("task_id")
        if typ == "offload_response":
            seq = message["client_seq"]
            self.event("admission_response_receive", task_id=task_id, client_seq=seq,
                       accepted=message["accepted"], status=message["status"],
                       request_response_ms=(time.monotonic_ns() - self.request_start_ns[seq]) / 1e6 if seq in self.request_start_ns else None)
            if task_id not in self.state["task_ids"]:
                self.state["task_ids"].append(task_id)
                self.save()
            future = self.pending.pop(seq, None)
            if future and not future.done():
                future.set_result(message)
        elif typ == "error":
            future = self.pending.pop(message.get("client_seq"), None)
            if future and not future.done():
                future.set_exception(ValueError(message.get("detail", message["reason"])))
        elif typ == "frame_result":
            key = (task_id, message["frame_id"])
            captured = self.capture_times.pop(key, None)
            now = time.monotonic_ns()
            latency_ms = (now - captured[1]) / 1e6 if captured is not None else None
            tx_latency_ms = (now - captured[2]) / 1e6 if captured is not None else None
            met = message["status"] == "completed" and latency_ms is not None and latency_ms <= self.slos.get(task_id, float("inf"))
            self.event("result_receive", task_id=task_id, frame_id=message["frame_id"],
                       job_seq=message["job_seq"], result_id=message["result_id"], status=message["status"],
                       capture_to_result_ms=latency_ms, transmission_to_result_ms=tx_latency_ms, slo_met=met)
        elif typ in {"task_completed", "task_expired", "task_failed", "task_cancelled"}:
            self.terminals[task_id] = message
            waiter = self.terminal_waiters.get(task_id)
            if waiter and not waiter.done():
                waiter.set_result(message)
            self.event("task_terminal", task_id=task_id, status=message.get("status"), reason=message.get("reason"))
        self.received.put_nowait(message)

    async def request(self, task_type, *, latency_ms=200, input_type="video", can_drop=False, can_drop_frames=False, parameters=None):
        await self.connected.wait()
        seq = self.state["next_client_seq"]
        self.state["next_client_seq"] += 1
        self.save()
        if self.ip is None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((urlparse(self.url).hostname, 9))
                self.ip = probe.getsockname()[0]
        request = {"type": "offload_request", "ue_id": self.state["ue_id"], "client_seq": seq,
                   "ip_address": self.ip, "task_type": task_type, "input_type": input_type,
                   "slo": {"latency_ms": latency_ms, "latency_origin": "capture"},
                   "can_drop": can_drop, "can_drop_frames": can_drop_frames,
                   "parameters": parameters or {}, "ue_request_send_unix_ns": time.time_ns()}
        future = asyncio.get_running_loop().create_future()
        self.pending[seq], self.requests[seq] = future, request
        self.request_start_ns[seq] = time.monotonic_ns()
        self.event("request_send", client_seq=seq)
        try:
            await self.socket.send(json.dumps(request))
        except ConnectionClosed:
            pass  # _connections retries this same client_seq after mec_hello.
        response = await future
        self.slos[response["task_id"]] = latency_ms
        return response

    async def end(self, task_id, final_frame_id):
        while True:
            await self.connected.wait()
            try:
                await self.socket.send(json.dumps({"type": "task_end", "ue_id": self.state["ue_id"],
                    "task_id": task_id, "final_frame_id": final_frame_id}))
                self.event("task_end_send", task_id=task_id, final_frame_id=final_frame_id)
                return
            except ConnectionClosed:
                await asyncio.sleep(0.1)

    async def wait_terminal(self, task_id, timeout=60):
        if task_id in self.terminals:
            return self.terminals[task_id]
        future = self.terminal_waiters.setdefault(task_id, asyncio.get_running_loop().create_future())
        return await asyncio.wait_for(asyncio.shield(future), timeout)

    async def stream(self, response, frames=60, video=None):
        app, endpoint = response["app_config"], response["data_plane"]
        task_id = response["task_id"]
        codec = app["codec"]
        encoder = H264Encoder(app) if codec == "h264" else None
        source = None
        if video:
            import cv2
            source = cv2.VideoCapture(video)
            if not source.isOpened():
                raise ValueError("Could not open video")
        sequence, ssrc = secrets.randbits(16), secrets.randbits(32)
        final = -1
        clock = time.monotonic()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.setblocking(False)
                for frame_id in range(frames):
                    if task_id in self.terminals:
                        break
                    await asyncio.sleep(max(0, clock + frame_id / app["fps"] - time.monotonic()))
                    if source:
                        import cv2
                        ok, bgr = await asyncio.to_thread(source.read)
                        if not ok:
                            break
                        rgb = cv2.cvtColor(cv2.resize(bgr, (app["width"], app["height"])), cv2.COLOR_BGR2RGB)
                    else:
                        rgb = np.full((app["height"], app["width"], 3), frame_id % 256, np.uint8)
                    capture = time.time_ns()  # Application frame availability/release, not original recording date.
                    capture_mono = time.monotonic_ns()
                    if encoder:
                        encoded = await asyncio.to_thread(encoder.encode, rgb, frame_id)
                    else:
                        out = io.BytesIO()
                        Image.fromarray(rgb).save(out, format="JPEG", quality=85)
                        encoded = out.getvalue()
                    tx = time.time_ns()
                    self.capture_times[(task_id, frame_id)] = (capture, capture_mono, time.monotonic_ns())
                    header = Header(task_id, bytes.fromhex(endpoint["media_token"]), frame_id, capture, tx)
                    self.event("frame_tx_start", task_id=task_id, frame_id=frame_id, ue_capture_unix_ns=capture,
                               capture_boundary="application_frame_available", ue_tx_start_unix_ns=tx)
                    for packet in packetize(header, encoded, codec, sequence=sequence, timestamp=round(frame_id * 90000 / app["fps"]),
                                            ssrc=ssrc, mtu=endpoint["mtu"]):
                        await asyncio.get_running_loop().sock_sendto(udp, packet, (endpoint["host"], endpoint["port"]))
                        sequence = (sequence + 1) & 65535
                    final = frame_id
            if task_id not in self.terminals:
                await self.end(task_id, final)
        finally:
            if source:
                source.release()
            if encoder:
                encoder.close()

    async def close(self):
        if self.connection_task:
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
        self.save()
        self.events.close()


class H264Encoder:
    def __init__(self, app):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst, self.fps = Gst, app["fps"]
        self.pipeline = Gst.parse_launch(
            f'appsrc name=input format=time block=true max-buffers=1 max-bytes=0 max-time=0 '
            f'caps="video/x-raw,format=RGB,width={app["width"]},height={app["height"]},framerate={self.fps}/1" ! '
            f'videoconvert ! x264enc tune=zerolatency speed-preset=veryfast bframes=0 rc-lookahead=0 sync-lookahead=0 '
            f'threads=1 key-int-max={self.fps} bitrate={app["bitrate_kbps"]} ! '
            'video/x-h264,stream-format=byte-stream,alignment=au ! h264parse config-interval=-1 ! '
            'appsink name=encoded max-buffers=1 drop=false sync=false enable-last-sample=false wait-on-eos=false')
        self.source = self.pipeline.get_by_name("input")
        self.sink = self.pipeline.get_by_name("encoded")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("H.264 encoder did not start")

    def encode(self, rgb, frame_id):
        raw = rgb.tobytes()
        buffer = self.Gst.Buffer.new_allocate(None, len(raw), None)
        buffer.fill(0, raw)
        buffer.pts = round(frame_id * 1e9 / self.fps)
        buffer.duration = round(1e9 / self.fps)
        if self.source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
            raise RuntimeError("H.264 encoder rejected frame")
        sample = self.sink.emit("try-pull-sample", 5 * self.Gst.SECOND)
        if sample is None:
            raise RuntimeError("H.264 encoder timed out")
        encoded = sample.get_buffer()
        return encoded.extract_dup(0, encoded.get_size())

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


async def demo(args):
    client = UEClient(args.url, args.ip, args.state, args.log)
    try:
        await client.start()
        response = await client.request(args.task_type, latency_ms=args.latency_ms,
                                        can_drop=args.can_drop, can_drop_frames=args.can_drop_frames)
        print(json.dumps(response, indent=2))
        if response["accepted"]:
            await client.stream(response, args.frames, args.video)
            print(json.dumps(await client.wait_terminal(response["task_id"]), indent=2))
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws/ue")
    parser.add_argument("--ip")
    parser.add_argument("--task-type", default="frame_statistics")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--video", help="Prerecorded video; optional OpenCV dependency")
    parser.add_argument("--latency-ms", type=float, default=200)
    parser.add_argument("--can-drop", action="store_true")
    parser.add_argument("--can-drop-frames", action="store_true")
    parser.add_argument("--state", default="ue_state.json")
    parser.add_argument("--log", default="ue_events.jsonl")
    args = parser.parse_args()
    asyncio.run(demo(args))


if __name__ == "__main__":
    main()
