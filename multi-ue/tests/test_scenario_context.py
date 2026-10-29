import asyncio
import json
from pathlib import Path

import pytest
import yaml

from mec_server.config import load
from mec_server.engine import Engine
from mec_server.recording import Recorder
from muApp_mec.runtime import Runtime


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {
    "campaign_id": "campaign-a",
    "scenario_id": "scenario-001",
    "attempt_id": "attempt-001",
}


def test_recorder_freezes_context_when_record_is_enqueued(tmp_path):
    context = dict(CONTEXT)
    recorder = Recorder(tmp_path, lambda: dict(context))
    recorder.event("first")
    context["attempt_id"] = "attempt-002"
    recorder.event("second")
    recorder.close()

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert events[0]["attempt_id"] == "attempt-001"
    assert events[1]["attempt_id"] == "attempt-002"


def test_collection_lifecycle_requires_armed_exact_context(tmp_path):
    cfg, catalog, profiles = load(ROOT / "config/mec.development.yaml")
    cfg.mode = "collection"
    cfg.runs_dir = str(tmp_path)
    engine = Engine(cfg, catalog, profiles)
    try:
        with pytest.raises(ValueError, match="scenario_not_armed"):
            engine.validate_message_context({})
        assert engine.arm_scenario(CONTEXT)["status"] == "armed"
        engine.validate_message_context(CONTEXT)
        with pytest.raises(ValueError, match="scenario_context_mismatch"):
            engine.validate_message_context(dict(CONTEXT, attempt_id="wrong"))
        _, hello = engine.hello(None, "12.1.1.2", lambda _: None)
        assert {key: hello[key] for key in CONTEXT} == CONTEXT
    finally:
        engine.record.close()


def test_edgeric_sync_stamps_events_with_context(tmp_path):
    async def exercise():
        config = yaml.safe_load((ROOT / "config/edgeric.shadow.yaml").read_text())
        config["runs_dir"] = str(tmp_path)
        runtime = Runtime(config)
        response = await runtime.exchange(
            dict(
                schema_version=1,
                type="sync",
                run_id="run-a",
                awareness_revision=1,
                associations=[],
                sessions=[],
                **CONTEXT,
            )
        )
        assert response["status"] == "synced"
        event = runtime.log_queue.get_nowait()
        assert {key: event[key] for key in CONTEXT} == CONTEXT
        stale = await runtime.exchange(
            dict(
                schema_version=1,
                type="sync",
                run_id="run-a",
                awareness_revision=0,
                associations=[],
                sessions=[],
                **dict(CONTEXT, attempt_id="stale-attempt"),
            )
        )
        assert stale["status"] == "stale"
        assert runtime.attempt_id == CONTEXT["attempt_id"]

    asyncio.run(exercise())
