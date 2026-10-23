"""Validated controls for the configurable experiment layout."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mec_server.config import Settings
from mec_server.engine import Engine, Video
from mec_server.policies import HeuristicPriority


def settings(**changes):
    values = dict(
        run_id="test",
        mode="development",
        catalog="tasks.yaml",
        profiles="profiles.yaml",
        backend="mock",
        warmup_count=1,
        radio_budget=1.0,
        compute_budget_s_per_s=1.0,
        global_queue_initial_s=0.001,
        profile_compatibility="test",
    )
    values.update(changes)
    return Settings(**values)


def test_layout_defaults_preserve_current_experiment():
    cfg = settings()
    cfg.check()

    assert cfg.fps == 30 and cfg.gop_frames == 30
    assert cfg.applications == ("detection", "tracking", "segmentation")
    assert cfg.priority_streams == (-5, -3, 0)
    assert cfg.keyframe_interleaving.enabled


def test_layout_accepts_ablation_settings_and_rejects_ambiguous_streams():
    cfg = settings(
        fps=24,
        gop_frames=48,
        applications=["detection"],
        priority_streams=[-5, 0],
        keyframe_interleaving={"enabled": False},
    )
    cfg.check()
    assert not cfg.keyframe_interleaving.enabled
    assert HeuristicPriority(cfg.priority_streams).propose(1, 0.6, 0, 0, 0) == 0

    with pytest.raises(ValueError, match="priority streams"):
        settings(priority_streams=[0, -5]).check()


@pytest.mark.parametrize("enabled,expected_offsets", [(True, [0, 24]), (False, [0, 0])])
def test_plan_uses_configured_gop_and_optional_interleaving(
    monkeypatch, enabled, expected_offsets
):
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(
        fps=24,
        gop_frames=48,
        gop_lead_s=0,
        run_id="run",
        keyframe_interleaving=SimpleNamespace(enabled=enabled),
    )
    engine.epoch_unix_ns = 0
    engine.plan_version = 0
    engine.config_version = 0
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
    videos = [Video(f"video-{i}", f"ue-{i}", "detection", 1, i) for i in range(2)]
    monkeypatch.setattr("mec_server.engine.time.time_ns", lambda: 1_000_000_000)

    engine.plan(videos, {video.uid: "candidate" for video in videos})

    plans = [video.pending for video in videos]
    assert [plan["offset_frames"] for plan in plans] == expected_offsets
    assert all(plan["fps"] == 24 and plan["gop_frames"] == 48 for plan in plans)
