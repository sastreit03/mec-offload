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
    recovered = []
    runner.recover = lambda *args: recovered.append(args)

    runner.run()

    assert state.row("scenario-a") == ("interrupted", 1)
    assert len(recovered) == 1
    assert recovered[0][:2] == ("runner stopped", scenario)
    assert recovered[0][2].startswith("attempt-")


def test_wait_fails_after_media_progress_stalls(tmp_path, monkeypatch):
    context = {
        "campaign_id": "campaign-a",
        "scenario_id": "scenario-a",
        "attempt_id": "attempt-a",
    }
    status = {
        "scenario": context,
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
