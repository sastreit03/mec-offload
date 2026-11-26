"""Loopback campaign acceptance test. Run with the PYTHONPATH in run.sh."""
import asyncio
import hashlib
import json
import signal
import socket
import struct
import tempfile
import unittest
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from websockets.asyncio.server import serve
from mec_contracts.rtp import parse_packet, Assembly
from app import DEFAULT
from handset import envelope
from playback import load_frames

HERE = Path(__file__).resolve().parent


class Campaign(unittest.IsolatedAsyncioTestCase):
    async def test_campaign_and_manual_start(self):
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = dict(width=16,height=16,fps=10,gop_frames=10,bitrate_bps=100000,
                       config_version=1,plan_version=1)
            sha = "a"*64
            key = f"px1-{sha}-0-80-16x16-10-10-100000"
            data = struct.pack(">4sH",b"PAV1",len(key))+key.encode()+struct.pack(">I",80)
            for i in range(80):
                au = b"\0\0\0\1"+(b"\x65" if i%10==0 else b"\x41")+b"x"*2000
                data += struct.pack(">IB",len(au),int(i%10==0))+au
            (root/f"{key}-{hashlib.sha256(data).hexdigest()}.pav").write_bytes(data)
            ctx = dict(campaign_id="camp", scenario_id="scenario", attempt_id="attempt")
            vid = str(uuid.uuid4())
            visits = []
            done = loop.create_future()
            udp = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            self.addCleanup(udp.close)
            udp.bind(("127.0.0.1",0))
            udp.setblocking(False)
            ep = dict(host="127.0.0.1",port=udp.getsockname()[1])
            async def mec(ws):
                visits.append(1)
                async def send(kind, **kw):
                    await ws.send(json.dumps(dict(type=kind,**kw)))
                async def until(kind):
                    while True:
                        m = json.loads(await ws.recv())
                        if m["type"] == kind:
                            return m
                try:
                    await send("mec_hello", ue_id="u", connection_id="c", run_id="run", resume_token="secret",
                               collection_mode=True,input_profile=cfg)
                    await until("association_update")
                    import time
                    await send("association_probe",endpoint=ep,pattern=[1],start_unix_ns=time.time_ns(),
                               rate_bps=1_000_000,interval_s=.005,probe_id="p")
                    probe = await loop.sock_recv(udp,65535)
                    self.assertTrue(probe.startswith(b"MECP1 u c p 0"))
                    await send("association_status", valid=True)
                    await send("scenario_context",active=True,assignment=dict(application="segmentation",media_id="m",
                               media_path="absent.mp4",media_sha256=sha,candidate_id="b1",source_start_frame=0,
                               frame_count=10,bitrate_bps=100000),**ctx)
                    await until("video_start")
                    await send("video_response",accepted=True,video_id=vid,media_epoch=1,slo_ms=150,rtp=dict(mtu=1200),
                               endpoint=ep,config=cfg,**ctx)
                    await until("scenario_context_ack")
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(loop.sock_recv(udp,65535),.12)
                    await send("scenario_start",**ctx)
                    for i in range(4):
                        p = parse_packet(await loop.sock_recv(udp,65535))
                        self.assertEqual((p.descriptor.video_id,p.descriptor.frame_id),(vid,i))
                        assembly = Assembly(p.descriptor)
                        while not assembly.add(p):
                            p = parse_packet(await loop.sock_recv(udp,65535))
                        result = dict(video_id=vid,frame_id=i,result_id=f"r{i}",outcome="completed",
                                      reason=None,**ctx,payload={"mask":"X"*30000})
                        await send("frame_result",**result)
                        await send("frame_result",**result)
                    await send("scenario_stop",**ctx)
                    end = await until("video_end")
                    self.assertEqual(end["frames_fully_sent"],4)
                    await send("video_status",video_id=vid,status="COMPLETED",**ctx)
                    done.set_result(True)
                    await ws.wait_closed()
                except Exception as e:
                    if not done.done():
                        done.set_exception(e)
            def request(path, body=None):
                headers={"Origin":url,"Content-Type":"application/json"}
                req=urllib.request.Request(url+path,headers=headers,data=None if body is None else json.dumps(body).encode())
                with urllib.request.urlopen(req,timeout=10) as r:
                    return json.load(r) if path.startswith("/api") and path != "/api/events" else r.read()
            async with serve(mec,"127.0.0.1",0) as server:
                port=server.sockets[0].getsockname()[1]
                with socket.socket() as s:
                    s.bind(("127.0.0.1",0))
                    ui=s.getsockname()[1]
                url=f"http://127.0.0.1:{ui}"
                for cycle in range(2):
                    p=await asyncio.create_subprocess_exec("sh",str(HERE/"run.sh"),"--state-dir",tmp,"--ui-port",str(ui),
                                                           stdout=asyncio.subprocess.PIPE)
                    try:
                        await asyncio.wait_for(p.stdout.readline(),5)
                        await asyncio.to_thread(request,"/")
                        status=await asyncio.to_thread(request,"/api/status")
                        self.assertFalse(status["active"])
                        await asyncio.sleep(.15)
                        self.assertEqual(len(visits),cycle)
                        if cycle==0:
                            await asyncio.to_thread(request,"/api/connect",DEFAULT|dict(folder=tmp,server="127.0.0.1",port=port))
                            with self.assertRaises(urllib.error.HTTPError) as active_export:
                                await asyncio.to_thread(request,"/api/events")
                            self.assertEqual(active_export.exception.code, 400)
                            await asyncio.wait_for(done,8)
                            await asyncio.to_thread(request,"/")
                            s=await asyncio.to_thread(request,"/api/status")
                            self.assertEqual(s["health"]["association"],"Validated")
                            self.assertEqual(s["health"]["rtp"],"Completed result received")
                            await asyncio.to_thread(request,"/api/disconnect",{})
                            exported = await asyncio.to_thread(request,"/api/events")
                            self.assertEqual(exported, next(root.rglob("events.jsonl")).read_bytes())
                            exported_rows = [json.loads(line) for line in exported.splitlines()]
                            self.assertEqual(sum(r["type"] == "result_receipt" for r in exported_rows), 4)
                    finally:
                        p.send_signal(signal.SIGTERM)
                        self.assertEqual(await asyncio.wait_for(p.wait(),8),0)
            rows=[json.loads(x) for x in next(root.rglob("events.jsonl")).read_text().splitlines()]
            sent=[r for r in rows if r["type"]=="transmitted"]
            result=[r for r in rows if r["type"]=="result_receipt"]
            self.assertEqual(len(sent),len(result))
            self.assertEqual(len(sent),4)
            for tx,rx in zip(sent,result):
                self.assertLessEqual(tx["scheduled_release_mono_ns"],tx["tx_mono_ns"])
                self.assertLessEqual(tx["tx_mono_ns"],tx["last_udp_send_return_mono_ns"])
                self.assertEqual(rx["latency_ns"],rx["receipt_mono_ns"]-tx["tx_mono_ns"])
            self.assertTrue(any(r["type"]=="cpu_sample" and r["attempt_id"]=="attempt" for r in rows))

    def test_wrong_asset_digest(self):
        cfg = dict(width=16, height=16, fps=1, gop_frames=1, bitrate_bps=100)
        sha = "e"*64
        assignment = dict(media_sha256=sha, source_start_frame=0, frame_count=8, bitrate_bps=100)
        key = f"px1-{sha}-0-8-16x16-1-1-100"
        data = struct.pack(">4sH", b"PAV1", len(key)) + key.encode() + struct.pack(">I", 8)
        data += (struct.pack(">IB", 5, 1) + bytes.fromhex("0000000165")) * 8
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / (key + "-" + "0"*64 + ".pav")).write_bytes(data)
            with self.assertRaisesRegex(ValueError, "digest"):
                load_frames(path, assignment, cfg)

    def test_metadata_and_legacy_replay(self):
        m=dict(type="frame_result",campaign_id="c",scenario_id="s",attempt_id="a",frame_id=0,
               result_id="r",video_id="v",outcome="completed",reason=None)
        self.assertEqual(envelope(json.dumps(m)[:-1]+', "payload":invalid'),m)
        self.assertEqual(envelope(json.dumps({"payload": [1,2]}|m))["result_id"],"r")


if __name__=="__main__":
    unittest.main()
