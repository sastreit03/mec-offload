"""Suspension boundaries and cross-component transmission-control behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from mec_server.engine import Engine, Video
from mec_ue.media import Streamer
from muApp_mec.runtime import Runtime


def test_mec_suspension_uses_next_phased_gop_boundary(monkeypatch):
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(
        gop_lead_s=1.0,
        run_id="run",
        fps=30,
        gop_frames=30,
        keyframe_interleaving=SimpleNamespace(enabled=True),
    )
    engine.send = Mock()
    engine.aware = Mock()
    engine.record = Mock()

    video = Video("video", "ue", "detection", 1, 0)
    video.pending = dict(
        epoch_unix_ns=1_000_000_000,
        offset_frames=10,
        fps=30,
        gop_frames=30,
        boundary_unix_ns=10_333_333_333,
        plan_version=7,
    )
    monkeypatch.setattr("mec_server.engine.time.time_ns", lambda: 10_100_000_000)

    engine.suspend_video(video)

    # The first phase boundary at least one second in the future is 11 1/3 s.
    assert video.suspension_boundary == 11_333_333_333
    assert video.state == "SUSPENDED"
    message = engine.send.call_args.args[1]
    assert message["effective_unix_ns"] == video.suspension_boundary
    assert message["plan_version"] == 7
    assert message["control_version"] == video.suspension_version == 1
    awareness = engine.aware.call_args.kwargs["transmission_control"]
    assert awareness == video.transmission_control


def test_resume_plan_is_newer_and_carries_the_suspension_interval(monkeypatch):
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(
        gop_lead_s=1.0,
        run_id="run",
        fps=30,
        gop_frames=30,
        keyframe_interleaving=SimpleNamespace(enabled=True),
    )
    engine.epoch_unix_ns = 1_000_000_000
    engine.plan_version = 7
    engine.config_version = 10
    engine.catalog = {"version": "catalog", "source_hw": [720, 1280]}
    engine.candidates = {
        "candidate": dict(
            id="candidate",
            application="detection",
            model="model",
            bitrate_bps=1_000_000,
            input_hw=[640, 640],
            precision="fp16",
            residence_s=0.01,
            frame_bytes_I=1000,
            frame_bytes_P=100,
        )
    }
    engine.residence = {}
    engine.send = Mock()
    engine.aware = Mock()

    video = Video("video", "ue", "detection", 1, 0, slo=0.15)
    video.suspension_boundary = 11_333_333_333
    video.suspension_version = 4
    video.control_version = 4
    video.applied = dict(
        candidate_id="candidate",
        first_frame_id=10,
        first_tx_unix_ns=10_000_000_000,
        boundary_unix_ns=10_000_000_000,
        fps=30,
    )
    monkeypatch.setattr("mec_server.engine.time.time_ns", lambda: 11_000_000_000)

    engine.plan([video], {"ue": "candidate"})

    command = engine.send.call_args.args[1]
    assert command["control_version"] == 5
    assert command["suspension_version"] == 4
    assert command["stop_unix_ns"] == 11_333_333_333
    assert command["boundary_unix_ns"] > command["stop_unix_ns"]
    assert command["first_frame_id"] == 70


def test_edgeric_stops_predictions_but_retains_actual_demand():
    runtime = Runtime.__new__(Runtime)
    runtime.cfg = {"max_sessions": 64}
    runtime.sessions = {}
    runtime.retired_sessions = {}
    runtime.source = "source"
    expectation = dict(
        applied=True,
        control_version=1,
        config_version=1,
        plan_version=1,
        first_tx_unix_ns=1_000_000_000,
        first_frame_id=0,
        fps=10,
        gop_frames=10,
        network_budget_ns=100_000_000,
        size_profile={"I": 1000, "P": 100},
    )
    session = dict(
        ue_id="ue", video_id="video", media_epoch=1, expectation=expectation
    )
    runtime._session(session)
    runtime.predict(3_500_000_000)
    ledger = runtime.sessions["video"]["ledger"]
    assert set(ledger.frames) == set(range(26))

    # Frame 22 is real traffic and must survive removal of later predictions.
    ledger.descriptor(22, 3_200_000_000, None, 150, True, 1)
    suspension = dict(
        control_version=2,
        suspension_version=2,
        command_id="suspend",
        status="SUSPENDED",
        effective_unix_ns=3_000_000_000,
        stop_unix_ns=3_000_000_000,
        plan_version=1,
    )
    runtime._session(dict(session, transmission_control=suspension))
    runtime.predict(6_000_000_000)
    assert set(ledger.frames) == set(range(20)) | {22}
    assert ledger.frames[22].actual

    # A future resume supplies provisional demand at its advertised activation.
    resume = dict(
        control_version=3,
        suspension_version=2,
        command_id="resume",
        status="ACTIVE",
        effective_unix_ns=5_000_000_000,
        stop_unix_ns=3_000_000_000,
        plan_version=2,
    )
    pending = dict(
        expectation,
        applied=False,
        control_version=3,
        config_version=2,
        plan_version=2,
        first_tx_unix_ns=5_000_000_000,
        first_frame_id=40,
    )
    runtime._session(
        dict(session, expectation=pending, transmission_control=resume)
    )
    runtime.predict(5_100_000_000)
    assert set(range(40, 42)) <= set(ledger.frames)
    assert all(not ledger.frames[fid].actual for fid in range(40, 42))

    # A delayed suspension cannot replace the newer resume control.
    runtime._session(dict(session, transmission_control=suspension))
    assert runtime.sessions["video"]["transmission_control"] == resume

    applied = dict(
        expectation,
        control_version=3,
        config_version=2,
        plan_version=2,
        first_tx_unix_ns=5_000_000_000,
        first_frame_id=40,
    )

    async def apply_descriptor():
        runtime.run_id = "run"
        await runtime.exchange(
            dict(
                schema_version=1,
                type="descriptor",
                run_id="run",
                ue_id="ue",
                video_id="video",
                media_epoch=1,
                frame_id=40,
                tx_unix_ns=5_000_000_000,
                network_deadline_unix_ns=None,
                encoded_bytes=150,
                config_version=2,
                applied_expectation=applied,
            )
        )

    asyncio.run(apply_descriptor())
    runtime.predict(5_100_000_000)
    assert ledger.frames[40].actual and 41 in ledger.frames


def test_linux_client_rejects_reordered_suspension_after_resume():
    events = []
    response = dict(
        video_id="video",
        config=dict(control_version=1, config_version=1, boundary_unix_ns=0),
    )
    clock = SimpleNamespace(unix_ns=lambda: 100)
    streamer = Streamer(
        response, "source", "127.0.0.1", clock, lambda *args: events.append(args)
    )
    suspension = dict(
        command_id="suspend",
        control_version=2,
        effective_unix_ns=200,
    )
    resume = dict(
        command_id="resume",
        control_version=3,
        suspension_version=2,
        stop_unix_ns=200,
        config_version=2,
        boundary_unix_ns=300,
    )

    streamer.suspend(suspension)
    streamer.command(resume)
    streamer.suspend(suspension)

    assert streamer.control_version == 3
    assert streamer.pause_at == 200 and streamer.pause_version == 2
    assert streamer.pending == [resume]
    assert len(events) == 1


def test_linux_client_rejects_plan_after_actual_boundary_stall(monkeypatch):
    events = []
    response = dict(
        video_id="video",
        media_epoch=1,
        endpoint=dict(host="127.0.0.1", port=5000),
        rtp=dict(mtu=1200),
        config=dict(
            command_id="initial",
            control_version=1,
            config_version=1,
            plan_version=1,
            boundary_unix_ns=0,
            width=16,
            height=16,
            fps=10,
            bitrate_bps=100_000,
            gop_frames=10,
        ),
    )

    class Clock:
        def __init__(self):
            self.calls = 0

        def unix_ns(self):
            self.calls += 1
            # The command arrives early, but the media loop resumes after a stall.
            return 900_000_000 if self.calls == 1 else 1_400_000_000

    class Capture:
        def isOpened(self):
            return True

        def read(self):
            return False, None

        def release(self):
            pass

    sock = Mock()
    sock.getsockname.return_value = ("127.0.0.1", 1234)
    monkeypatch.setitem(
        __import__("sys").modules,
        "cv2",
        SimpleNamespace(VideoCapture=lambda _source: Capture()),
    )
    monkeypatch.setattr("mec_ue.media.Encoder", Mock())
    monkeypatch.setattr("mec_ue.media.socket.socket", lambda *args: sock)

    streamer = Streamer(
        response, "source", "127.0.0.1", Clock(), lambda *args: events.append(args)
    )
    streamer.command(
        dict(
            response["config"],
            command_id="next",
            control_version=2,
            config_version=2,
            plan_version=2,
            boundary_unix_ns=1_000_000_000,
        )
    )
    streamer.run()

    assert streamer.current["config_version"] == 1
    assert streamer.pending == []
    assert (
        "plan_unapplied",
        dict(command_id="next", reason="boundary_missed"),
    ) in events


def test_linux_suspension_skips_source_frames_and_preserves_frame_gap(monkeypatch):
    """Paused source frames consume IDs without changing the last transmitted ID."""
    events = []
    response = dict(
        video_id="video",
        media_epoch=1,
        endpoint=dict(host="127.0.0.1", port=5000),
        rtp=dict(mtu=1200),
        config=dict(
            control_version=1,
            config_version=1,
            plan_version=1,
            boundary_unix_ns=1,
            width=16,
            height=16,
            fps=10,
            bitrate_bps=100_000,
            gop_frames=10,
        ),
    )

    class Capture:
        def __init__(self, _source):
            self.skipped = 0

        def isOpened(self):
            return True

        def grab(self):
            self.skipped += 1
            # End the media loop after three suspended source frames.
            return self.skipped <= 3

        def release(self):
            pass

    capture = Capture("source")
    monkeypatch.setitem(
        __import__("sys").modules,
        "cv2",
        SimpleNamespace(VideoCapture=lambda _source: capture),
    )
    monkeypatch.setattr("mec_ue.media.Encoder", Mock())
    monkeypatch.setattr("mec_ue.media.socket.socket", Mock())

    streamer = Streamer(
        response,
        "source",
        "127.0.0.1",
        SimpleNamespace(unix_ns=lambda: 1_000_000_000),
        lambda *args: events.append(args),
    )
    streamer.pause_at = 1
    streamer.run()

    assert streamer.frame_id == 3
    assert streamer.last_frame == -1
    assert capture.skipped == 4


def test_mec_records_skipped_resume_ids_as_suspended():
    engine = Engine.__new__(Engine)
    engine.resolve_missing = Mock()
    video = Video("video", "ue", "tracking", 1, 0)
    video.last_descriptor_id = 20
    video.suspension_boundary = 1_000
    video.suspension_version = 2

    resumed_plan = dict(control_version=3)
    engine.resolve_suspension_gap(video, 24, resumed_plan)

    assert [c.args[1:] for c in engine.resolve_missing.call_args_list] == [
        (21, "session_suspended", "dropped"),
        (22, "session_suspended", "dropped"),
        (23, "session_suspended", "dropped"),
    ]
    assert video.last_descriptor_id == 24
    assert video.suspension_gap_version == 3

    # Later packet gaps in the same active plan remain transport-loss candidates.
    engine.resolve_suspension_gap(video, 26, resumed_plan)
    assert engine.resolve_missing.call_count == 3
