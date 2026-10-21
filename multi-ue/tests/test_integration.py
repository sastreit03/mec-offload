import asyncio
import copy
import time
from pathlib import Path
import socket
import numpy as np
import yaml
import pytest
from mec_server.config import load
from mec_server.engine import Engine
from mec_contracts.rtp import Descriptor
from muApp_mec.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mock_workers_grpc_lifecycle_replay_and_fencing(tmp_path):
    async def exercise():
        cfg, catalog, profiles = load(ROOT / "config/mec.development.yaml")
        cfg.runs_dir = str(tmp_path / "mec")
        cfg.edgeric_target = f"127.0.0.1:{port()}"
        edgecfg = yaml.safe_load((ROOT / "config/edgeric.shadow.yaml").read_text())
        edgecfg.update(
            listen=cfg.edgeric_target,
            runs_dir=str(tmp_path / "edge"),
            trace_file=str(tmp_path / "absent.csv"),
        )
        edge = Runtime(edgecfg)
        et = asyncio.create_task(edge.run())
        e = Engine(cfg, catalog, profiles)
        messages = []
        await e.start()
        try:
            for _ in range(300):
                if e.readiness()["ready"]:
                    break
                await asyncio.sleep(0.02)
            assert e.readiness()["ready"], e.readiness()
            u, h = e.hello(None, "12.1.1.1", messages.append)
            m = dict(
                type="video_start",
                ue_id=u.uid,
                client_seq=1,
                application="detection",
                slo_ms=150,
                input=dict(width=1280, height=720, fps=30),
            )
            r = await e.command(u, m)
            assert r["accepted"]
            vid = r["video_id"]
            v = e.videos[vid]
            assert (await e.command(u, m)) == r
            assert (await e.command(u, dict(m, application="tracking")))[
                "reason"
            ] == "conflicting_client_seq"
            assert not (await e.command(u, dict(m, client_seq=2)))["accepted"]
            c = r["config"]
            d = Descriptor(
                vid,
                r["media_epoch"],
                0,
                0,
                c["config_version"],
                c["plan_version"],
                1,
                100,
                time.monotonic_ns(),
                time.monotonic_ns(),
                time.time_ns(),
                time.time_ns(),
                15,
            )
            e.receive_descriptor(d, time.monotonic_ns())
            assert e.frames[(vid, 0)].state == "RECEIVING"
            for _ in range(100):
                if vid in edge.sessions and 0 in edge.sessions[vid]["ledger"].frames:
                    break
                await asyncio.sleep(0.01)
            assert (
                0 in edge.sessions[vid]["ledger"].frames
            )  # Descriptor forwarded before full reception/decode.
            e.progress_event(d, 100, True, time.monotonic_ns())
            e.decoded(d, np.zeros((72, 128, 3), dtype=np.uint8), time.monotonic_ns())
            for _ in range(100):
                if any(x["type"] == "frame_result" for x in messages):
                    break
                await asyncio.sleep(0.01)
            result = next(x for x in messages if x["type"] == "frame_result")
            assert result["outcome"] == "completed"
            assert (
                not edge.sessions[vid]["ledger"].frames
                or edge.sessions[vid]["ledger"].frames.get(0) is None
            )
            old = u.generation
            u2, h2 = e.hello(h["resume_token"], "12.1.1.3", messages.append)
            assert (
                u2.uid == u.uid
                and u2.active_video == vid
                and h2["connection_id"] != h["connection_id"]
            )
            e.disconnect(u.uid, old)
            assert u2.connected
            rows = e.replay.ordered_page(u.uid, e.replay.maximum(u.uid), 0, 100)
            assert rows[0]["result_id"] == result["result_id"]
            end = await e.command(
                u2,
                dict(
                    type="video_end",
                    ue_id=u.uid,
                    video_id=vid,
                    client_seq=3,
                    final_frame_id=2,
                ),
            )
            assert end["type"] == "video_end_ack"
            await asyncio.sleep(0.7)
            missing = [
                x
                for x in messages
                if x["type"] == "frame_result" and x["frame_id"] in (1, 2)
            ]
            assert len(missing) == 2 and all(
                x["metadata_available"] is False for x in missing
            )
            assert v.finalized and u.active_video is None
            assert "rnti" not in str(e.ues) and len(e.workers.ready) == 3
        finally:
            await e.close()
            et.cancel()
            await asyncio.gather(et, return_exceptions=True)

    asyncio.run(exercise())


def test_experimental_profiles_fail_closed():
    with pytest.raises(ValueError, match="required profiled setting"):
        load(ROOT / "config/mec.experimental.yaml")
