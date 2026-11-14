import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "run_campaign", ROOT / "tools/run_campaign.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_checkpoint_resumes_without_repeating_valid_scenario(tmp_path):
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    scenario = {"scenario_id": "scenario-a", "execution_index": 0}
    state.register(scenario)
    state.start(scenario, "attempt-a")
    state.finish("scenario-a", "valid", evidence={"frames": 150})
    assert state.row("scenario-a") == ("valid", 1)

    reopened = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    assert reopened.row("scenario-a") == ("valid", 1)


def test_checkpoint_marks_only_running_attempt_interrupted(tmp_path):
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    scenario = {"scenario_id": "scenario-a", "execution_index": 0}
    state.register(scenario)
    state.start(scenario, "attempt-a")
    reopened = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    assert reopened.row("scenario-a") == ("interrupted", 1)


def test_operator_stop_checkpoints_and_clears_armed_attempt(tmp_path):
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    scenario = {"scenario_id": "scenario-a", "execution_index": 0}
    runner = MODULE.Runner(
        SimpleNamespace(output=str(tmp_path), max_attempts=3), [scenario], state
    )
    runner.attempt = lambda *_: (_ for _ in ()).throw(
        MODULE.RunnerStopped("runner stopped")
    )
    runner.await_preflight = lambda *_: {}
    recovered = []
    runner.recover = lambda *args: recovered.append(args)

    runner.run()

    assert state.row("scenario-a") == ("interrupted", 1)
    assert len(recovered) == 1
    assert recovered[0][:2] == ("runner stopped", scenario)
    assert recovered[0][2].startswith("attempt-")


def test_completed_pilot_clears_final_assignment(tmp_path):
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    scenario = {"scenario_id": "scenario-a", "execution_index": 0}
    runner = MODULE.Runner(
        SimpleNamespace(output=str(tmp_path), max_attempts=3), [scenario], state
    )
    runner.attempt = lambda *_: {"frames": 150}
    runner.await_preflight = lambda *_: {}
    disarmed = []
    runner.disarm = lambda *args: disarmed.append(args)
    recovered = []
    runner.recover = lambda *args: recovered.append(args)

    runner.run()

    assert state.row("scenario-a") == ("valid", 1)
    assert recovered == []
    assert len(disarmed) == 1
    assert disarmed[0][0] == scenario
    assert disarmed[0][1].startswith("attempt-")


def test_association_failure_rejects_attempt_and_drains_without_restart(tmp_path, monkeypatch):
    context = dict(campaign_id="campaign", scenario_id="scenario", attempt_id="attempt")
    status = dict(scenario=context, scenario_error=dict(reason="association_lost"), scenario_idle=True)
    monkeypatch.setattr(MODULE, "http_json", lambda *_: status)
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest")
    runner = MODULE.Runner(SimpleNamespace(
        output=str(tmp_path), mec="http://mec", scenario_timeout=1, poll_interval=0,
    ), [], state)
    scenario = dict(campaign_id="campaign", scenario_id="scenario", ues=[])
    with pytest.raises(RuntimeError, match="collection scenario invalid") as error:
        runner.wait(scenario, "attempt")
    disarmed = []
    runner.disarm = lambda *args: disarmed.append(args)
    runner.recover = lambda *_: pytest.fail("healthy drained attempt must not restart services")
    runner.retire_failed_attempt(str(error.value), scenario, "attempt")
    assert disarmed == [(scenario, "attempt")]


@pytest.mark.parametrize("confirmed", [True, False])
def test_attempt_requires_final_confirmation(tmp_path, monkeypatch, confirmed):
    context = dict(campaign_id="campaign", scenario_id="scenario", attempt_id="attempt")
    requests = []
    def http(_base, path, value):
        requests.append(path)
        if path.endswith("/arm"):
            return dict(status="armed")
        if path.endswith("/start"):
            assert "lead_s" not in value
            return dict(status="started", start_unix_ns=1, queue_loop_ns=1)
        assert path.endswith("/finalize")
        return dict(context, status="finalized" if confirmed else "rejected")
    monkeypatch.setattr(MODULE, "http_json", http)
    runner = MODULE.Runner(SimpleNamespace(
        output=str(tmp_path), mec="http://mec",
    ), [], MODULE.State(tmp_path / "state.sqlite3", "manifest"))
    runner.select_channel = lambda _: None
    runner.await_assignment_ack = lambda *_: None
    runner.wait = lambda *_: dict(videos={})
    scenario = dict(campaign_id="campaign", scenario_id="scenario", ues=[], channel={})
    if confirmed:
        result = runner.attempt(scenario, "attempt", dict(awareness_generation="mec"))
        assert result["confirmation"]["attempt_id"] == "attempt"
    else:
        with pytest.raises(RuntimeError, match="scenario finalization failed"):
            runner.attempt(scenario, "attempt", dict(awareness_generation="mec"))
    assert requests == ["/api/scenario/arm", "/api/scenario/start", "/api/scenario/finalize"]


def test_wait_fails_after_media_progress_stalls(tmp_path, monkeypatch):
    context = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "attempt_id": "attempt-a",
    }
    status = {
        "scenario": context,
        "ues": {
            "ue-a": {"alias": "pixel1", "connected": True},
        },
        "videos": {
            "video-a": {
                "ue_alias": "pixel1",
                "state": "ACTIVE",
                "counters": {"completed": 134, "dropped": 0, "failed": 0},
                "final_frame_id": None,
                "collection_error": None,
            }
        },
        "unresolved": 4,
    }
    monkeypatch.setattr(MODULE, "http_json", lambda *_: status)
    monkeypatch.setattr(MODULE.time, "sleep", lambda *_: None)
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path),
            mec="http://mec.test",
            scenario_timeout=1,
            stall_timeout=0.01,
            poll_interval=0,
        ),
        [],
        state,
    )
    scenario = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "ues": [
            {
                "ue_alias": "pixel1",
                "candidate_id": "candidate-a",
                "frame_count": 150,
            }
        ],
    }

    with pytest.raises(RuntimeError, match="scenario stalled"):
        runner.wait(scenario, "attempt-a")


def test_wait_detects_lost_gnb_channel_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "channel_request",
        lambda *_: (_ for _ in ()).throw(RuntimeError("controller unavailable")),
    )
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path),
            mec="http://mec.test",
            channel_endpoint="tcp://gnb.test:5560",
            scenario_timeout=10,
            stall_timeout=5,
            disconnect_grace=0,
            poll_interval=0,
        ),
        [],
        state,
    )
    scenario = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "ues": [],
    }

    with pytest.raises(RuntimeError, match="gNB channel controller unavailable"):
        runner.wait(scenario, "attempt-a", {"condition_id": "awgn-sinr20"})


def test_pass_through_requires_direct_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "channel_request",
        lambda *_: {
            "source": "direct",
            "condition_id": "pass-through",
            "trace_sha256": "",
            "generation": 0,
        },
    )
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path), channel_endpoint="tcp://gnb.test:5560",
            trace_bank=str(tmp_path),
        ),
        [],
        state,
    )

    status = runner.select_channel(
        {"channel": {"condition_id": "pass-through"}}
    )
    assert status["source"] == "direct"


def test_wait_detects_required_ue_disconnect(tmp_path, monkeypatch):
    context = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "attempt_id": "attempt-a",
    }
    monkeypatch.setattr(
        MODULE,
        "http_json",
        lambda *_: {
            "scenario": context,
            "ues": {},
            "videos": {},
            "unresolved": 0,
        },
    )
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path),
            mec="http://mec.test",
            scenario_timeout=10,
            stall_timeout=5,
            disconnect_grace=0,
            poll_interval=0,
        ),
        [],
        state,
    )
    scenario = {
        **context,
        "ues": [
            {
                "ue_alias": "linux1",
                "candidate_id": "candidate-a",
                "frame_count": 150,
            }
        ],
    }

    with pytest.raises(RuntimeError, match="linux1"):
        runner.wait(scenario, "attempt-a")


def test_wait_allows_transient_ue_reconnect(tmp_path, monkeypatch):
    context = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "attempt_id": "attempt-a",
    }
    statuses = iter([
        {"scenario": context, "ues": {}, "videos": {}, "unresolved": 0},
        {
            "scenario": context,
            "ues": {"ue-a": {"alias": "pixel1", "connected": True}},
            "videos": {"video-a": {
                "ue_alias": "pixel1", "state": "COMPLETED",
                "candidate_id": "candidate-a", "final_frame_id": 0,
                "counters": {"completed": 1, "dropped": 0, "failed": 0},
                "collection_error": None,
            }},
            "unresolved": 0,
        },
    ])
    monkeypatch.setattr(MODULE, "http_json", lambda *_: next(statuses))
    monkeypatch.setattr(MODULE.time, "sleep", lambda *_: None)
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path), mec="http://mec.test", scenario_timeout=10,
            stall_timeout=5, disconnect_grace=10, poll_interval=0,
        ),
        [], state,
    )
    scenario = {
        **context,
        "ues": [{
            "ue_alias": "pixel1", "candidate_id": "candidate-a",
            "frame_count": 1,
        }],
    }

    result = runner.wait(scenario, "attempt-a")
    assert result["videos"]["video-a"]["state"] == "COMPLETED"


def test_wait_classifies_zero_frame_expiration_as_media_not_started(
    tmp_path, monkeypatch
):
    context = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "attempt_id": "attempt-a",
    }
    monkeypatch.setattr(
        MODULE,
        "http_json",
        lambda *_: {
            "scenario": context,
            "ues": {
                "ue-a": {"alias": "pixel1", "connected": True},
            },
            "videos": {
                "video-a": {
                    "ue_alias": "pixel1",
                    "state": "EXPIRED",
                    "candidate_id": "candidate-a",
                    "final_frame_id": None,
                    "counters": {"completed": 0, "dropped": 0, "failed": 0},
                    "collection_error": None,
                }
            },
            "unresolved": 0,
        },
    )
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path), mec="http://mec.test", scenario_timeout=10,
            stall_timeout=5, disconnect_grace=10, poll_interval=0,
        ),
        [], state,
    )
    scenario = {
        **context,
        "ues": [{
            "ue_alias": "pixel1", "candidate_id": "candidate-a",
            "frame_count": 150,
        }],
    }

    with pytest.raises(RuntimeError, match="pixel1: media never started"):
        runner.wait(scenario, "attempt-a")


def test_failed_attempt_cleanup_reason_requests_mec_reset(tmp_path):
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(output=str(tmp_path), retry_delay=0), [], state
    )
    runner.disarm = lambda *_: (_ for _ in ()).throw(
        RuntimeError("POST /api/scenario/disarm failed (409): MEC must be idle")
    )
    recovered = []
    runner.recover = lambda *args: recovered.append(args)
    scenario = {"scenario_id": "scenario-a"}

    runner.retire_failed_attempt("UE disconnected", scenario, "attempt-a")

    assert len(recovered) == 1
    assert "MEC must be idle" in recovered[0][0]


def test_assignment_ack_requires_every_expected_ue(tmp_path, monkeypatch):
    context = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "attempt_id": "attempt-a",
    }
    monkeypatch.setattr(
        MODULE,
        "http_json",
        lambda *_: {
            "scenario": context,
            "ues": {
                "ue-a": {
                    "alias": "pixel1",
                    "connected": True,
                    "scenario_acknowledged": True,
                },
                "ue-b": {
                    "alias": "linux1",
                    "connected": True,
                    "scenario_acknowledged": False,
                },
            },
        },
    )
    state = MODULE.State(tmp_path / "state.sqlite3", "manifest-a")
    runner = MODULE.Runner(
        SimpleNamespace(
            output=str(tmp_path),
            mec="http://mec.test",
            assignment_timeout=-1,
            poll_interval=0,
        ),
        [],
        state,
    )
    scenario = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "ues": [{"ue_alias": "pixel1"}, {"ue_alias": "linux1"}],
    }

    with pytest.raises(RuntimeError, match="linux1"):
        runner.await_assignment_ack(scenario, "attempt-a")
