"""Validated controls for the configurable experiment layout."""

from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from mec_server.config import Settings, collection_candidates, load, qualified
from mec_server.engine import Engine, Frame, Video
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


def test_collection_mode_requires_fixed_candidates_but_not_heuristic_budgets():
    fixed = {
        "detection": "detector",
        "tracking": "tracker",
        "segmentation": "segmenter",
    }
    cfg = settings(
        mode="collection",
        backend="torchscript",
        priority_streams=[-3],
        gop_policy="fixed",
        priority_policy="fixed",
        fixed_candidates=fixed,
        radio_budget=None,
        compute_budget_s_per_s=None,
        global_queue_initial_s=None,
    )
    cfg.check()

    with pytest.raises(ValueError, match="fixed candidate"):
        cfg.model_copy(update={"fixed_candidates": {}}).check()
    with pytest.raises(ValueError, match="priority stream -3"):
        cfg.model_copy(update={"priority_streams": (-5, -3, 0)}).check()


def test_measured_candidate_is_not_filtered_by_relative_quality():
    candidate = dict(
        id="detection-model-500k",
        application="detection",
        model="model",
        input_hw=[640, 640],
        precision="fp16",
    )
    profile = dict(
        qualified=True,
        score=0.1,
        reference_score=1.0,
        processing_s=0.01,
        residence_s=0.02,
        frame_bytes_I=1000,
        frame_bytes_P=100,
    )

    result = qualified(
        {"configurations": [candidate]},
        {"candidates": {candidate["id"]: profile}},
    )

    assert candidate["id"] in result


def test_collection_candidate_uses_execution_measurements_without_quality_gate():
    candidate = dict(
        id="fixed",
        application="detection",
        model="model",
        input_hw=[640, 640],
        precision="fp16",
    )
    profile = dict(
        qualified=False,
        score=0.01,
        reference_score=1.0,
        processing_s=0.01,
        residence_s=0.02,
        frame_bytes_I=1000,
        frame_bytes_P=100,
    )

    result = collection_candidates(
        {"configurations": [candidate]},
        {"candidates": {"fixed": profile}},
    )

    assert result["fixed"]["processing_s"] == 0.01


def test_collection_load_does_not_require_network_filter_calibration(tmp_path):
    catalog = dict(
        version="test",
        codec="H264",
        source_hw=[720, 1280],
        fps=30,
        gop_frames=30,
        configurations=[
            dict(
                id="fixed",
                application="detection",
                model="model",
                artifact="model.torchscript",
                input_hw=[640, 640],
                precision="fp16",
                bitrate_bps=500_000,
                confidence=0.1,
                class_map_id="coco80",
                output_schema="detection.v1",
                backend="torchscript",
            )
        ],
    )
    profiles = dict(
        compatibility="measured",
        synthetic=False,
        network={key: None for key in (
            "mean_s",
            "P0_s2",
            "R_s2",
            "q_s2_per_s",
            "V_frame_s2",
            "radio_bps_per_unit",
        )},
        candidates={
            "fixed": dict(
                processing_s=0.01,
                residence_s=0.02,
                frame_bytes_I=1000,
                frame_bytes_P=100,
            )
        },
    )
    (tmp_path / "tasks.yaml").write_text(yaml.safe_dump(catalog))
    (tmp_path / "profiles.yaml").write_text(yaml.safe_dump(profiles))
    (tmp_path / "mec.yaml").write_text(
        yaml.safe_dump(
            dict(
                run_id="test",
                mode="collection",
                catalog="tasks.yaml",
                profiles="profiles.yaml",
                backend="torchscript",
                applications=["detection"],
                priority_streams=[-3],
                gop_policy="fixed",
                priority_policy="fixed",
                fixed_candidates={"detection": "fixed"},
                warmup_count=1,
                profile_compatibility="measured",
            )
        )
    )

    cfg, _, _ = load(tmp_path / "mec.yaml")

    assert cfg.radio_budget is None and cfg.global_queue_initial_s is None


def test_collection_uses_fixed_candidate_and_fifo_priority():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(
        mode="collection",
        fixed_candidates={"detection": "fixed"},
        priority_streams=(-3,),
        pending_frames=192,
        pending_bytes_per_video=512 * 1024 * 1024,
    )
    engine.candidates = {
        "fixed": dict(
            id="fixed",
            application="detection",
            model="model",
            bitrate_bps=500_000,
        )
    }
    engine.quarantined = set()
    engine.record = Mock()
    video = Video("video", "ue", "detection", 1, 0, state="ACTIVE")

    assert engine.choose([video]) == {"ue": "fixed"}

    descriptor = SimpleNamespace(video_id="video", frame_id=7, tx_unix_ns=1)
    frame = Frame(
        "video",
        7,
        descriptor,
        1,
        engine.candidates["fixed"],
        complete_ns=2,
        deadline_ns=1,
    )
    video.input_closed = True
    engine.frames = {("video", 7): frame}
    engine.videos = {"video": video}
    engine.pending = {("detection", -3): deque()}
    engine.queue_history = SimpleNamespace(value=0.0)
    rgb = SimpleNamespace(nbytes=1)

    engine.decoded(descriptor, rgb, 3)

    assert frame.state == "QUEUED" and frame.priority == -3
    assert list(engine.pending[("detection", -3)]) == [("video", 7)]


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
