"""Exercise GoP recovery with the real selector and joint resource budgets."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mec_server.engine import Engine, Video
from mec_server.policies import HeuristicGoP
from mec_server.statistics import History, NetworkFilter


@pytest.fixture
def coordinator():
    # Isolate coordination from sockets, recording threads, and GPU processes.
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(
        mode="development",
        fps=30,
        radio_budget=1.0,
        compute_budget_s_per_s=1.0,
        gop_lead_s=1.0,
        run_id="resumption-test",
    )
    network = dict(
        mean_s=0.01,
        P0_s2=0.001,
        R_s2=0.001,
        q_s2_per_s=0.0001,
        V_frame_s2=0.001,
        radio_bps_per_unit=1_000_000,
    )
    engine.profiles = {"network": network}
    engine.candidates = {
        "candidate": dict(application="detection", bitrate_bps=1_000_000)
    }
    engine.histories = {"candidate": (History(0.01), History(0))}
    engine.quarantined = set()
    engine.last_report = None
    engine.last_usable = {}
    engine.policy = HeuristicGoP()
    engine.record = Mock()
    engine.send = Mock()
    engine.aware = Mock()
    engine.plan = Mock()
    engine.usable_radio = Mock(return_value=True)
    engine.videos = {}
    engine.ues = {}

    def add_video(uid, admitted, state="SUSPENDED"):
        video = Video(uid, uid, "detection", 1, admitted, state=state)
        video.pending = dict(
            epoch_unix_ns=0, offset_frames=0, fps=30, gop_frames=30,
            boundary_unix_ns=0, plan_version=1,
        )
        engine.videos[uid] = video
        engine.ues[uid] = SimpleNamespace(uid=uid, network=NetworkFilter(network))
        return video

    return engine, add_video


def test_only_video_resumes_after_budget_recovers(coordinator):
    engine, add_video = coordinator
    video = add_video("only", 1, "ACTIVE")
    engine.cfg.radio_budget = 0.5
    engine.coordinate()
    assert video.state == "SUSPENDED"
    engine.plan.assert_not_called()
    decision = next(
        call
        for call in engine.record.event.call_args_list
        if call.args == ("gop_decision",)
    )
    assert decision.kwargs["decision_type"] == "gop_bitrate_dnn"
    assert decision.kwargs["decision_latency_ns"] >= 0

    engine.cfg.radio_budget = 1.0
    engine.coordinate()
    assert video.state == "ACTIVE"
    engine.plan.assert_called_once_with([video], {"only": "candidate"})
    # Selecting a resume plan is not evidence that the UE applied it.
    assert video.applied is None


@pytest.mark.parametrize(
    "budget,expected",
    [(0.5, []), (1.0, ["old"]), (2.0, ["old", "new"])],
)
def test_all_suspended_resumption_respects_joint_budget(coordinator, budget, expected):
    engine, add_video = coordinator
    # Insertion order differs from admission order to exercise oldest-first recovery.
    add_video("new", 2)
    add_video("old", 1)
    engine.cfg.radio_budget = budget
    engine.coordinate()
    resumed = sorted(v.uid for v in engine.videos.values() if v.state == "ACTIVE")
    assert resumed == sorted(expected)
    if expected:
        videos, proposal = engine.plan.call_args.args
        assert [v.uid for v in videos] == expected
        assert proposal == {uid: "candidate" for uid in expected}
        assert len(videos) == len({v.uid for v in videos})
    else:
        engine.plan.assert_not_called()


def test_unusable_telemetry_blocks_resumption(coordinator):
    engine, add_video = coordinator
    video = add_video("only", 1)
    engine.usable_radio.return_value = False
    engine.coordinate()
    assert video.state == "SUSPENDED"
    engine.usable_radio.assert_called_once_with("only", admission=True)
    engine.plan.assert_not_called()


def test_resumption_includes_existing_active_resource_cost(coordinator):
    engine, add_video = coordinator
    active = add_video("active", 1, "ACTIVE")
    suspended = add_video("waiting", 2)
    engine.coordinate()
    assert suspended.state == "SUSPENDED"
    engine.plan.assert_called_once_with([active], {"active": "candidate"})

    engine.plan.reset_mock()
    engine.cfg.radio_budget = 2.0
    engine.coordinate()
    assert suspended.state == "ACTIVE"
    engine.plan.assert_called_once_with(
        [active, suspended], {"active": "candidate", "waiting": "candidate"}
    )


def test_no_videos_produces_no_plan(coordinator):
    engine, _ = coordinator
    engine.coordinate()
    engine.plan.assert_not_called()
