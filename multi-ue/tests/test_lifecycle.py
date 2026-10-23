"""Offline regression checks for terminal video lifecycles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from mec_server.engine import Engine, UE, Video


def test_new_end_sequence_cannot_reopen_completed_video():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(run_id="run", final_arrival_s=1.0)
    engine.dedup = {}
    engine.record = Mock()
    user = UE("ue", "token", active_video=None)
    video = Video("video", user.uid, "detection", 1, 0)
    video.state = "COMPLETED"
    video.finalized = True
    video.final_id = 7
    video.input_close_at = 10.0
    engine.videos = {video.vid: video}

    result = asyncio.run(
        engine.command(
            user,
            dict(
                type="video_end",
                ue_id=user.uid,
                video_id=video.vid,
                client_seq=2,
                final_frame_id=99,
            ),
        )
    )

    assert result["type"] == "video_end_ack" and result["status"] == "COMPLETED"
    assert video.state == "COMPLETED" and video.finalized
    assert video.final_id == 7 and video.input_close_at == 10.0


def test_every_rejected_start_records_an_admission_decision():
    async def exercise():
        engine = Engine.__new__(Engine)
        engine.cfg = SimpleNamespace(run_id="run")
        engine.admission_lock = asyncio.Lock()
        engine.record = Mock()
        user = UE("ue", "token", active_video="active")
        engine.videos = {
            "active": Video("active", user.uid, "detection", 1, 0, state="ACTIVE")
        }

        result = await engine.start_video(
            user,
            dict(
                client_seq=4,
                application="detection",
                input=dict(width=1280, height=720, fps=30),
            ),
        )

        assert not result["accepted"] and result["reason"] == "active_video_exists"
        kind, = engine.record.event.call_args.args
        evidence = engine.record.event.call_args.kwargs
        assert kind == "admission_decision" and not evidence["accepted"]
        assert evidence["decision_latency_ns"] >= 0

    asyncio.run(exercise())
