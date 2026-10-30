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
        assert engine.disarm_scenario(CONTEXT)["status"] == "disarmed"
        assert engine.scenario is None
        assert engine.disarm_scenario(CONTEXT)["status"] == "already_disarmed"
    finally:
        engine.record.close()


def test_ue_acknowledges_exact_scenario_assignment(tmp_path):
    cfg, catalog, profiles = load(ROOT / "config/mec.development.yaml")
    cfg.mode = "collection"
    cfg.runs_dir = str(tmp_path)
    engine = Engine(cfg, catalog, profiles)
    sent = []
    try:
        user, _ = engine.hello(None, "12.1.1.2", sent.append)
        asyncio.run(
            engine.command(
                user,
                {
                    "type": "association_update",
                    "ue_alias": "pixel1",
                    "ue_ip": "12.1.1.2",
                },
            )
        )
        candidate_id, candidate = next(iter(engine.candidates.items()))
        armed = dict(
            CONTEXT,
            ues=[
                {
                    "ue_alias": "pixel1",
                    "application": candidate["application"],
                    "candidate_id": candidate_id,
                    "bitrate_bps": candidate["bitrate_bps"],
                    "frame_count": 150,
                    "source_start_frame": 0,
                }
            ],
        )
        engine.arm_scenario(armed)
        assert user.scenario_ack is None
        response = asyncio.run(
            engine.command(user, {"type": "scenario_context_ack", **CONTEXT})
        )
        assert response["type"] == "scenario_context_acknowledged"
        assert user.scenario_ack == (CONTEXT["attempt_id"], user.generation, "pixel1")
        started = engine.start_scenario({**CONTEXT, "lead_s": 1.0})
        assert started["status"] == "started"
        assert started["start_unix_ns"] > 0
        assert started["release_delay_ns"] == 1_000_000_000
        start_message = next(
            message for message in sent if message["type"] == "scenario_start"
        )
        assert start_message["release_delay_ns"] == 1_000_000_000
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
                awareness_generation="mec-process-a",
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
                awareness_generation="mec-process-a",
                awareness_revision=0,
                associations=[],
                sessions=[],
                **dict(CONTEXT, attempt_id="stale-attempt"),
            )
        )
        assert stale["status"] == "stale"
        assert runtime.attempt_id == CONTEXT["attempt_id"]

    asyncio.run(exercise())


def test_edgeric_accepts_revision_reset_from_restarted_mec(tmp_path):
    async def exercise():
        config = yaml.safe_load((ROOT / "config/edgeric.shadow.yaml").read_text())
        config["runs_dir"] = str(tmp_path)
        runtime = Runtime(config)
        common = dict(
            schema_version=1,
            type="sync",
            run_id="fixed-run-id",
            sessions=[],
            **CONTEXT,
        )
        await runtime.exchange(
            dict(
                common,
                awareness_generation="old-process",
                awareness_revision=99,
                associations=[dict(ue_id="old", connection_id="a", ip="12.1.1.2")],
            )
        )
        response = await runtime.exchange(
            dict(
                common,
                awareness_generation="new-process",
                awareness_revision=0,
                associations=[dict(ue_id="new", connection_id="b", ip="12.1.1.3")],
            )
        )
        assert response["status"] == "synced"
        assert set(runtime.associations.bindings) == {"new"}
        assert runtime.sync_revision == 0
        runtime.associations.bindings["new"].rnti = 1234
        runtime.associations.bindings["new"].valid_until = 999999999.0
        await runtime.exchange(
            dict(
                common,
                awareness_generation="new-process",
                awareness_revision=1,
                associations=[
                    dict(ue_id="new", connection_id="b", ip="12.1.1.3")
                ],
            )
        )
        assert runtime.associations.bindings["new"].rnti == 1234
        delayed = await runtime.exchange(
            dict(
                common,
                awareness_generation="old-process",
                awareness_revision=100,
                associations=[],
            )
        )
        assert delayed["status"] == "stale_generation"
        assert set(runtime.associations.bindings) == {"new"}

    asyncio.run(exercise())
