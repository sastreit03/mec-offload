"""CPU tests use real WebSockets, UDP packets and spawned reusable processes."""
import asyncio
import io
import importlib.util
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import numpy as np
from PIL import Image
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from mec.config import Settings, Catalog, AppConfig
from mec.control import ControlHub
from mec.decision import Telemetry
from mec.frames import FIFO, Frame, FrameQueue
from mec.records import stamp
from mec.rtp import Header, parse, packetize
from mec.service import MEC
from mec.ue import UEClient


class UnitTests(unittest.TestCase):
    def test_rtp_metadata_and_wraparound(self):
        header = Header(str(uuid4()), b"a" * 16, 2 ** 32 + 4, time.time_ns(), time.time_ns())
        parts = list(packetize(header, b"z" * 9000, "jpeg", sequence=65534,
                              timestamp=2 ** 32 + 30, ssrc=14, mtu=1200))
        self.assertTrue(all(len(p) <= 1200 for p in parts))
        self.assertEqual(parse(parts[2])[2], 0)
        for part in reversed(parts):
            self.assertEqual(parse(part)[0], header)
            self.assertEqual(parse(part)[3], 30)
        self.assertTrue(parse(parts[-1])[5])
        with self.assertRaises(ValueError):
            parse(parts[0][:30])

    def test_h264_packetization_fu_a(self):
        header = Header(str(uuid4()), b"b" * 16, 0, 1, 2)
        encoded = b"\x00\x00\x00\x01\x67abc\x00\x00\x01\x65" + b"z" * 5000
        packets = list(packetize(header, encoded, "h264", sequence=10, timestamp=99, ssrc=12))
        payloads = [parse(p)[1] for p in packets]
        self.assertEqual(payloads[0], b"\x67abc")
        self.assertEqual(payloads[1][0] & 31, 28)
        self.assertTrue(payloads[1][1] & 128)
        self.assertTrue(payloads[-1][1] & 64)
        self.assertEqual(b"".join(p[2:] for p in payloads[1:]), b"z" * 5000)

    def test_eight_frame_overflow_excludes_inflight(self):
        dropped = []
        frames = FrameQueue(8, FIFO(), lambda f, reason: dropped.append((f.frame_id, reason)))
        for fid in range(10):
            frames.put(Frame(fid, np.zeros((2, 2, 3), np.uint8), 1, 2, {}))
        self.assertEqual(dropped, [(0, "buffer_overflow"), (1, "buffer_overflow")])
        self.assertEqual([frames.pop().frame_id for _ in range(8)], list(range(2, 10)))
        self.assertFalse(frames.put(Frame(9, np.zeros((2, 2, 3), np.uint8), 1, 2, {})))

    def test_telemetry_uses_source_period(self):
        cfg = Settings().ric
        telemetry = Telemetry(cfg)
        period = int(time.time_ns() // 10 ** 9)
        telemetry.update({"report_unix_ns": (period - 1) * 10 ** 9, "rnti": 123, "ue_id": "app"})
        self.assertTrue(telemetry.snapshot()["fresh"])
        self.assertNotIn("rnti", telemetry.latest)
        telemetry.update({"report_unix_ns": (period - 3) * 10 ** 9})
        self.assertFalse(telemetry.snapshot()["fresh"])
        telemetry.update({"cached": "no source timestamp"})
        self.assertFalse(telemetry.snapshot()["fresh"])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.cfg = Settings()
        self.cfg.logs_dir = self.tmp.name
        self.cfg.network.bind_host = "127.0.0.1"
        self.cfg.network.media_bind_host = "127.0.0.1"
        self.cfg.network.media_port_first = port
        self.cfg.network.media_port_last = min(port + 20, 65535)
        self.cfg.timeouts.first_media_s = 8
        self.cfg.timeouts.preparation_s = 6
        self.cfg.timeouts.execution_ready_s = 10
        self.cfg.timeouts.disconnect_grace_s = 2
        self.cfg.timeouts.final_frame_arrival_grace_s = 0.15
        self.cfg.snapshot_interval_s = 0.2
        self.app = AppConfig(config_id="mock", task_type="demo", width=32, height=24,
                             warmup_iterations=1, options={"infer_delay_s": 0.01})
        self.mec = MEC(self.cfg, Catalog(configurations=[self.app]))
        self.hub = ControlHub(self.mec)
        self.runner = asyncio.create_task(self.mec.run())
        self.server = await serve(self.hub.session, "127.0.0.1", 0, max_size=None, compression=None)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/ws/ue"
        self.sockets = []
        self.seq = 0

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        self.server.close()
        await self.server.wait_closed()
        self.runner.cancel()
        result = await asyncio.gather(self.runner, return_exceptions=True)
        await self.mec.close()
        self.tmp.cleanup()
        if isinstance(result[0], Exception) and not isinstance(result[0], asyncio.CancelledError):
            raise result[0]

    async def open(self, token=None):
        ws = await connect(self.url, additional_headers={"X-MEC-Resume-Token": token} if token else {}, max_size=None, proxy=None)
        self.sockets.append(ws)
        hello = json.loads(await ws.recv())
        return ws, hello

    async def receive(self, ws, predicate, timeout=10):
        async with asyncio.timeout(timeout):
            while True:
                value = json.loads(await ws.recv())
                if predicate(value):
                    return value

    async def until(self, predicate, timeout=10):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    async def request(self, ws, hello, seq=None, **updates):
        seq = self.seq if seq is None else seq
        self.seq += 1
        raw = {"type": "offload_request", "ue_id": hello["ue_id"], "client_seq": seq,
               "ip_address": "12.1.1.2", "task_type": "demo", "input_type": "video",
               "slo": {"latency_ms": 200}, "can_drop": False, "can_drop_frames": False, **updates}
        await ws.send(json.dumps(raw))
        return await self.receive(ws, lambda m: m.get("client_seq") == seq and m["type"] in {"offload_response", "error"})

    def frame(self, response, fid, reverse=False, token=None):
        endpoint = response["data_plane"]
        out = io.BytesIO()
        Image.fromarray(np.full((24, 32, 3), fid % 255, np.uint8)).save(out, format="JPEG")
        h = Header(response["task_id"], bytes.fromhex(token or endpoint["media_token"]), fid, time.time_ns(), time.time_ns())
        packets = list(packetize(h, out.getvalue(), "jpeg", sequence=fid * 10, timestamp=fid * 6000, ssrc=33, mtu=300))
        if reverse:
            packets.reverse()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            for part in packets:
                udp.sendto(part, (endpoint["host"], endpoint["port"]))

    async def end(self, ws, hello, response, final):
        await ws.send(json.dumps({"type": "task_end", "ue_id": hello["ue_id"],
            "task_id": response["task_id"], "final_frame_id": final}))

    async def test_parent_frames_order_and_warm_reuse(self):
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        self.assertTrue(response["accepted"])
        self.assertEqual(response["status"], "preparing")
        self.assertFalse(response["queue_policy"]["can_drop_frames"])
        for fid in range(3):
            self.frame(response, fid, reverse=True)
        await self.end(ws, hello, response, 2)
        terminal = await self.receive(ws, lambda m: m["type"] == "task_completed")
        task = self.mec.tasks[response["task_id"]]
        self.assertEqual(task.counts["completed"], 3)
        self.assertEqual([r["frame_id"] for r in self.mec.recent_jobs], [0, 1, 2])
        wid = task.worker_id
        await self.until(lambda: self.mec.pool.idle("mock") is not None)
        second = await self.request(ws, hello)
        self.assertEqual(self.mec.tasks[second["task_id"]].worker_id, wid)
        self.frame(second, 0)
        await self.end(ws, hello, second, 0)
        await self.receive(ws, lambda m: m["type"] == "task_completed" and m["task_id"] == second["task_id"])

    async def test_two_ues_same_model_distinct_workers(self):
        a, ah = await self.open()
        b, bh = await self.open()
        ar, br = await asyncio.gather(self.request(a, ah), self.request(b, bh))
        self.frame(ar, 0)
        self.frame(br, 0)
        await self.until(lambda: self.mec.tasks[ar["task_id"]].counts["completed"] == 1 and self.mec.tasks[br["task_id"]].counts["completed"] == 1)
        self.assertNotEqual(ah["ue_id"], bh["ue_id"])
        self.assertNotEqual(self.mec.tasks[ar["task_id"]].worker_id, self.mec.tasks[br["task_id"]].worker_id)
        self.assertEqual(len(self.mec.pool.workers), 2)

    async def test_retry_identity_and_conflicting_seq(self):
        ws, hello = await self.open()
        response = await self.request(ws, hello, seq=42)
        retry = await self.request(ws, hello, seq=42, ip_address="12.1.1.3")
        self.assertEqual(response["task_id"], retry["task_id"])
        self.assertEqual(len(self.mec.tasks), 1)
        self.assertEqual(self.mec.ues[hello["ue_id"]].reported_ip, "12.1.1.3")
        error = await self.request(ws, hello, seq=42, can_drop=True)
        self.assertEqual(error["type"], "error")

    async def test_five_waiting_limit_includes_preparing(self):
        self.app.options["prepare_delay_s"] = 3
        ws, hello = await self.open()
        responses = [await self.request(ws, hello) for _ in range(6)]
        self.assertTrue(all(r["accepted"] for r in responses[:5]))
        self.assertFalse(responses[5]["accepted"])
        self.assertEqual(responses[5]["reason"], "capacity_limit")
        self.assertEqual(len(self.mec.waiting), 5)

    async def test_preparation_failure_after_acceptance(self):
        self.app.options["fail_prepare"] = True
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        self.frame(response, 0)
        failure = await self.receive(ws, lambda m: m["type"] == "task_failed")
        self.assertTrue(response["accepted"])
        self.assertEqual(failure["reason"], "preparation_failed")

    async def test_preparation_timeout(self):
        self.app.options["prepare_delay_s"] = 3
        self.cfg.timeouts.preparation_s = 0.3
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        self.frame(response, 0)
        failure = await self.receive(ws, lambda m: m["type"] == "task_failed")
        self.assertEqual(failure["reason"], "preparation_timeout")

    async def test_resource_timeout_with_busy_worker(self):
        self.cfg.pool.max_workers = 1
        ws, hello = await self.open()
        first = await self.request(ws, hello)
        self.frame(first, 0)
        await self.until(lambda: self.mec.tasks[first["task_id"]].counts["completed"] == 1)
        self.cfg.timeouts.worker_wait_s = 0.25
        second = await self.request(ws, hello)
        self.frame(second, 0)
        expired = await self.receive(ws, lambda m: m["type"] == "task_expired" and m["task_id"] == second["task_id"])
        self.assertEqual(expired["reason"], "resource_timeout")

    async def test_end_before_media_and_missing_range(self):
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        await self.end(ws, hello, response, 2)
        await asyncio.sleep(0.05)
        self.frame(response, 0)
        missing = await self.receive(ws, lambda m: m["type"] == "frames_missing")
        self.assertEqual((missing["frame_id_first"], missing["frame_id_last"]), (1, 2))
        terminal = await self.receive(ws, lambda m: m["type"] == "task_completed")
        self.assertEqual(terminal["reason"], "completed_with_missing_frames")

    async def test_disconnect_resume_replay_and_stale_timer(self):
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        await self.until(lambda: "execution_ready" in self.mec.tasks[response["task_id"]].times)
        await ws.close()
        self.frame(response, 0)
        await self.until(lambda: bool(self.hub.history.get(hello["ue_id"])))
        resumed, recovered = await self.open(hello["resume_token"])
        self.assertEqual(recovered["ue_id"], hello["ue_id"])
        self.assertNotEqual(recovered["connection_id"], hello["connection_id"])
        replay = await self.receive(resumed, lambda m: m["type"] == "frame_result")
        self.assertEqual(replay["result_id"], self.hub.history[hello["ue_id"]][0]["result_id"])
        self.assertEqual(self.mec.ues[hello["ue_id"]].connection_state, "connected")

    async def test_disconnect_expiry_does_not_revive_task(self):
        self.cfg.timeouts.disconnect_grace_s = 0.2
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        await ws.close()
        await self.until(lambda: self.mec.tasks[response["task_id"]].terminal)
        resumed, recovered = await self.open(hello["resume_token"])
        expired = await self.receive(resumed, lambda m: m["type"] == "task_expired")
        self.assertEqual(expired["reason"], "control_disconnect_timeout")
        self.assertTrue(self.mec.tasks[response["task_id"]].terminal)

    async def test_stale_media_token_and_no_input_timeout(self):
        self.cfg.timeouts.first_media_s = 0.2
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        self.frame(response, 0, token="00" * 16)
        expired = await self.receive(ws, lambda m: m["type"] == "task_expired")
        self.assertEqual(expired["reason"], "no_input_timeout")

    async def test_ric_failure_falls_back_and_admits_missing_telemetry(self):
        self.cfg.ric.decision_url = "http://127.0.0.1:1/decision"
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        self.assertTrue(response["accepted"])
        self.assertEqual(self.mec.tasks[response["task_id"]].decision_source, "heuristic")
        self.assertFalse(self.mec.telemetry.snapshot()["fresh"])

    async def test_matching_ue_client_logs_and_streams(self):
        client = UEClient(self.url, state_path=str(Path(self.tmp.name) / "ue.json"),
                          log_path=str(Path(self.tmp.name) / "ue.jsonl"))
        try:
            await client.start()
            response = await client.request("demo")
            await client.stream(response, frames=10)
            terminal = await client.wait_terminal(response["task_id"])
            self.assertEqual(terminal["type"], "task_completed")
            self.assertEqual(self.mec.tasks[response["task_id"]].counts["completed"], 10)
            records = [json.loads(line) for line in Path(self.tmp.name, "ue.jsonl").read_text().splitlines()]
            received = [r for r in records if r["event"] == "result_receive"]
            self.assertEqual(len(received), 10)
            self.assertTrue(all(r["capture_to_result_ms"] is not None for r in received))
        finally:
            await client.close()

    @unittest.skipUnless(importlib.util.find_spec("gi"), "GStreamer/PyGObject not installed")
    async def test_h264_gstreamer_roundtrip(self):
        self.app.codec = "h264"
        client = UEClient(self.url, state_path=str(Path(self.tmp.name) / "h264_ue.json"),
                          log_path=str(Path(self.tmp.name) / "h264_ue.jsonl"))
        try:
            await client.start()
            response = await client.request("demo")
            self.assertTrue(response["accepted"], response)
            await client.stream(response, frames=10)
            terminal = await client.wait_terminal(response["task_id"])
            self.assertEqual(terminal["type"], "task_completed")
            self.assertEqual(self.mec.tasks[response["task_id"]].counts["completed"], 10)
        finally:
            await client.close()

    async def test_malformed_json_keeps_control_connection_alive(self):
        ws, hello = await self.open()
        await ws.send("{not json")
        error = await self.receive(ws, lambda m: m["type"] == "error")
        self.assertEqual(error["reason"], "invalid_message")
        self.assertTrue((await self.request(ws, hello))["accepted"])

    async def test_drain_timeout_finishes_submitted_job_safely(self):
        self.app.options["infer_delay_s"] = 0.5
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        await self.until(lambda: "execution_ready" in self.mec.tasks[response["task_id"]].times)
        self.frame(response, 0)
        await self.until(lambda: self.mec.runtimes[response["task_id"]].in_flight is not None)
        self.cfg.timeouts.drain_s = 0.05
        await self.end(ws, hello, response, 0)
        expired = await self.receive(ws, lambda m: m["type"] == "task_expired")
        self.assertEqual(expired["reason"], "drain_timeout")
        await self.receive(ws, lambda m: m["type"] == "frame_result")
        self.assertEqual(self.mec.tasks[response["task_id"]].status, "expired")
        await self.until(lambda: self.mec.pool.idle("mock") is not None)

    async def test_worker_crash_records_failure(self):
        self.app.options["infer_delay_s"] = 0.5
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        await self.until(lambda: "execution_ready" in self.mec.tasks[response["task_id"]].times)
        self.frame(response, 0)
        await self.until(lambda: self.mec.runtimes[response["task_id"]].in_flight is not None)
        self.mec.runtimes[response["task_id"]].worker.process.kill()
        failure = await self.receive(ws, lambda m: m["type"] == "task_failed")
        self.assertEqual(failure["reason"], "execution_failed")
        self.assertEqual(self.mec.tasks[response["task_id"]].counts["failed"], 1)

    async def test_shutdown_snapshot_excludes_identity_tokens_and_outputs(self):
        ws, hello = await self.open()
        response = await self.request(ws, hello)
        self.frame(response, 0)
        self.mec.snapshot()
        await self.until(lambda: (self.mec.log.path / "run.json").exists())
        text = (self.mec.log.path / "run.json").read_text()
        self.assertNotIn(hello["resume_token"], text)
        self.assertNotIn('"rgb"', text)


if __name__ == "__main__":
    unittest.main()
