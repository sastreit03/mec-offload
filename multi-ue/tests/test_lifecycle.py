"""Offline regression checks for terminal video lifecycles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from mec_server.engine import (
    Engine,
    Frame,
    UE,
    Video,
    first_media_deadline,
    monotonic_start,
)


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


def test_collection_attempt_rejects_second_video_for_same_alias():
    async def exercise():
        engine = Engine.__new__(Engine)
        engine.cfg = SimpleNamespace(
            run_id="run", mode="collection", applications=("detection",)
        )
        engine.admission_lock = asyncio.Lock()
        engine.record = Mock()
        engine.videos = {}
        engine.scenario_assignments = {
            "pixel1": {
                "application": "detection",
                "candidate_id": "detection-yolo26m-2000k",
                "media_id": "media-a",
                "source_start_frame": 0,
                "frame_count": 150,
            }
        }
        engine.scenario_videos = {"pixel1": "prior-video"}
        engine.readiness = Mock(return_value={"ready": True})
        engine.usable_radio = Mock(return_value=True)
        user = UE("ue", "token", alias="pixel1")

        result = await engine.start_video(
            user,
            dict(
                client_seq=4,
                application="detection",
                candidate_id="detection-yolo26m-2000k",
                media_id="media-a",
                source_start_frame=0,
                frame_count=150,
            ),
        )

        assert not result["accepted"]
        assert result["reason"] == "scenario_already_started"

    asyncio.run(exercise())


def test_collection_admission_defers_first_media_timeout_until_scenario_start():
    """A synchronized release may be later than the ordinary media watchdog."""
    with (
        patch("mec_server.engine.time.monotonic", return_value=10.0),
        patch("mec_server.engine.time.time_ns", return_value=100_000_000_000),
    ):
        expected_start = monotonic_start(106_000_000_000)

    video = Video("video", "ue", "detection", 1, admitted=10.0)
    video.expected_start = expected_start

    assert expected_start == 16.0
    assert first_media_deadline(video, 5.0) == 21.0


def test_video_end_resolves_received_frames_that_decoder_cannot_emit():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(run_id="run", mode="collection", drain_s=120.0)
    engine.aware = Mock()
    engine.record = Mock()
    engine.task = Mock()
    engine.fail_frame = Mock()
    video = Video("video", "ue", "detection", 1, 0, state="DRAINING")
    incomplete = Frame(video.vid, 148, None, 0, {})
    complete = Frame(video.vid, 149, None, 0, {}, complete_ns=1)
    engine.frames = {
        (video.vid, incomplete.fid): incomplete,
        (video.vid, complete.fid): complete,
    }

    engine.close_input(video, "video_end")

    assert engine.fail_frame.call_args_list == [
        call(video.vid, 148, "transport_loss"),
        call(video.vid, 149, "decode_incomplete_after_video_end"),
    ]
