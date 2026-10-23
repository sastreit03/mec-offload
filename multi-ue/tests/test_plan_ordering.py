"""Offline checks for reordered plan evidence at the MEC."""

from types import SimpleNamespace
from unittest.mock import Mock

from mec_contracts.rtp import Descriptor
from mec_server.engine import Engine, UE, Video


def plan(version, first_frame, boundary):
    return dict(
        candidate_id=f"candidate-{version}",
        config_version=version,
        plan_version=version,
        control_version=version,
        epoch_unix_ns=0,
        offset_frames=0,
        activation_cycle=version,
        boundary_unix_ns=boundary,
        first_frame_id=first_frame,
        fps=30,
        gop_frames=30,
    )


def descriptor(frame, version, tx_ns):
    return Descriptor(
        "video",
        1,
        frame,
        0,
        version,
        version,
        1,
        1000,
        tx_unix_ns=tx_ns,
        flags=8,
    )


def engine_with_plans():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(
        max_clock_uncertainty_ns=1_000_000,
        allow_scheduled_throughput_estimate=False,
    )
    engine.frames = {}
    engine.residence = {}
    engine.record = Mock()
    engine.aware = Mock()
    engine.resolve_missing = Mock()
    engine.progress = {}
    engine.last_progress = {}
    engine.last_report = None
    engine.candidates = {
        f"candidate-{version}": dict(
            id=f"candidate-{version}",
            application="detection",
            model=f"model-{version}",
            bitrate_bps=1_000_000,
            input_hw=[640, 640],
            precision="fp16",
            residence_s=0.01,
            frame_bytes_I=1000,
            frame_bytes_P=100,
        )
        for version in (1, 2)
    }
    video = Video("video", "ue", "detection", 1, 0, state="ACTIVE")
    video.plans = {1: plan(1, 0, 1_000), 2: plan(2, 10, 2_000)}
    video.configs = {
        version: engine.candidates[f"candidate-{version}"] for version in (1, 2)
    }
    video.pending = video.plans[2]
    engine.videos = {video.vid: video}
    user = UE(video.uid, "token")
    user.network = SimpleNamespace(mu=0.01, observe=Mock(), propagate=Mock())
    engine.ues = {video.uid: user}
    return engine, video


def test_delayed_old_descriptor_does_not_roll_back_applied_plan():
    engine, video = engine_with_plans()
    first = descriptor(0, 1, 1_000)
    engine.receive_descriptor(first, 1)
    engine.receive_descriptor(descriptor(10, 2, 2_000), 2)
    engine.receive_descriptor(descriptor(9, 1, 1_900), 3)

    assert video.applied["plan_version"] == 2
    assert engine.frames[("video", 9)].config["id"] == "candidate-1"
    # EdgeRIC also receives the latest applied state with the delayed frame.
    awareness = engine.aware.call_args.kwargs
    assert awareness["plan_version"] == 1
    assert awareness["applied_expectation"]["plan_version"] == 2

    # The job record retains both network reception boundaries.
    assert engine.frames[("video", 0)].times["first_packet_monotonic_ns"] == 1
    engine.progress_event(first, 1000, True, 4)
    assert engine.frames[("video", 0)].times["complete_reception_monotonic_ns"] == 4


def test_old_plan_at_pending_boundary_marks_switch_unapplied():
    engine, video = engine_with_plans()
    engine.receive_descriptor(descriptor(0, 1, 1_000), 1)
    engine.receive_descriptor(descriptor(10, 1, 2_000), 2)

    assert video.applied["plan_version"] == 1
    assert video.pending is None
    engine.record.event.assert_any_call(
        "configuration_unapplied",
        video_id="video",
        frame_id=10,
        config_version=2,
        plan_version=2,
    )
