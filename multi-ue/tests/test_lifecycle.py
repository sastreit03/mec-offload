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


def test_video_end_promotes_prepared_successor_without_waiting_for_completion():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(run_id="run", final_arrival_s=0.5)
    engine.aware = Mock()
    engine.record = Mock()
    user = UE("ue", "token", active_video="old", prepared_video="next")
    old = Video("old", user.uid, "tracking", 1, 0, state="ACTIVE")
    nxt = Video(
        "next",
        user.uid,
        "tracking",
        2,
        0,
        state="PREPARED",
        predecessor_id="old",
    )
    engine.ues = {user.uid: user}
    engine.videos = {old.vid: old, nxt.vid: nxt}

    result = engine.end_video(
        user,
        dict(client_seq=7, video_id=old.vid, final_frame_id=12),
    )

    assert result["type"] == "video_end_ack"
    assert old.state == "DRAINING" and old.final_id == 12
    assert old.demand_closed
    assert nxt.state == "AWAITING_DELIVERY"
    assert user.active_video == nxt.vid and user.prepared_video is None


def test_first_successor_packet_can_promote_before_video_end_control():
    engine = Engine.__new__(Engine)
    engine.aware = Mock()
    engine.record = Mock()
    user = UE("ue", "token", active_video="old", prepared_video="next")
    old = Video("old", user.uid, "detection", 1, 0, state="ACTIVE")
    nxt = Video(
        "next",
        user.uid,
        "detection",
        2,
        0,
        state="PREPARED",
        predecessor_id="old",
    )
    engine.ues = {user.uid: user}
    engine.videos = {old.vid: old, nxt.vid: nxt}

    engine.promote_successor(nxt, "first_descriptor")

    assert old.state == "HANDOFF_PENDING_END" and old.demand_closed
    assert nxt.state == "AWAITING_DELIVERY"
    assert user.active_video == nxt.vid and user.prepared_video is None
