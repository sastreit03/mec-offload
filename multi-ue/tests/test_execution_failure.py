"""Candidate quarantine covers shared models and prevents new frame admission."""

from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

from mec_server.engine import Engine, Frame, Video


def candidate(candidate_id, model, bitrate):
    return dict(
        id=candidate_id,
        application="detection",
        model=model,
        input_hw=[640, 640],
        precision="fp16",
        bitrate_bps=bitrate,
    )


def test_model_failure_quarantines_all_bitrate_candidates():
    engine = Engine.__new__(Engine)
    engine.candidates = {
        "same-low": candidate("same-low", "same", 500_000),
        "same-high": candidate("same-high", "same", 1_000_000),
        "other": candidate("other", "other", 500_000),
    }
    engine.quarantined = set()
    engine.record = Mock()

    engine.quarantine_candidate("same-low", "CUDA failure")

    assert engine.quarantined == {"same-low", "same-high"}
    assert engine.record.event.call_args.args == ("candidate_quarantined",)


def test_quarantined_candidate_rejects_newly_decoded_frame():
    engine = Engine.__new__(Engine)
    descriptor = SimpleNamespace(video_id="video", frame_id=3)
    config = candidate("failed", "model", 500_000)
    frame = Frame("video", 3, descriptor, 1, config, complete_ns=2)
    video = Video("video", "ue", "detection", 1, 0, state="ACTIVE")
    engine.frames = {("video", 3): frame}
    engine.videos = {"video": video}
    engine.quarantined = {"failed"}
    engine.fail_frame = Mock()

    engine.decoded(descriptor, None, 3)

    engine.fail_frame.assert_called_once_with(
        "video", 3, "execution_candidate_quarantined"
    )


def test_priority_decision_has_separate_timing_evidence():
    engine = Engine.__new__(Engine)
    descriptor = SimpleNamespace(video_id="video", frame_id=3, tx_unix_ns=1)
    config = candidate("healthy", "model", 500_000)
    frame = Frame("video", 3, descriptor, 1, config, complete_ns=2)
    video = Video("video", "ue", "detection", 1, 0, state="ACTIVE", slo=1.0)
    engine.frames = {("video", 3): frame}
    engine.videos = {"video": video}
    engine.quarantined = set()
    engine.priority = SimpleNamespace(version="test", propose=lambda *args: -3)
    engine.queue_history = SimpleNamespace(value=0.0)
    engine.processing = Mock(return_value=0.01)
    engine.cfg = SimpleNamespace(pending_frames=8, pending_bytes_per_video=1024)
    engine.pending = {("detection", -3): deque()}
    engine.record = Mock()
    rgb = SimpleNamespace(nbytes=1)

    engine.decoded(descriptor, rgb, 3)

    assert frame.times["priority_decision_latency_ns"] >= 0
    evidence = engine.record.event.call_args.kwargs
    assert evidence["decision_type"] == "frame_priority"
    assert evidence["executed"] == -3
