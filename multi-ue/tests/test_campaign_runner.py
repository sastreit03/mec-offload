import importlib.util
from pathlib import Path
import sys


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
