import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mec_server.config import load
from mec_server.engine import Engine, Video
from mec_ue.client import Client
from muApp_mec.runtime import Runtime


CONTEXT = dict(campaign_id="campaign", scenario_id="scenario", attempt_id="attempt")


@pytest.fixture
def armed(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg, catalog, profiles = load(root / "config/mec.development.yaml")
    cfg.mode = "collection"
    cfg.runs_dir = str(tmp_path)
    engine = Engine(cfg, catalog, profiles)
    sent = []
    ue, _ = engine.hello(None, "12.1.1.2", sent.append)
    ue.alias = "pixel1"
    engine.arm_scenario(CONTEXT)
    engine.scenario_assignments = {"pixel1": {}}
    engine.scenario_report_source = "edge-a"
    yield engine, ue, sent
    engine.record.close()


def report(ue, **extra):
    return dict(
        source_generation="edge-a", window_end_unix_ns=time.time_ns(),
        association_losses={},
        ues={ue.uid: dict(association_valid=True, connection_id=ue.connection_id)},
        **extra,
    )


def test_recovered_association_still_aborts_and_fences_restart(armed):
    async def exercise():
        engine, ue, sent = armed
        video = Video("v", ue.uid, "detection", 1, 0, state="ACTIVE")
        engine.videos["v"] = video
        ue.active_video = "v"
        r = report(ue)
        r["association_losses"] = {ue.uid: dict(reason="lease_expired")}
        engine.check_scenario_radio(r)
        assert engine.scenario_error["reason"] == "association_lost"
        assert video.input_closed and video.state == "DRAINING"
        assert any(m["type"] == "scenario_abort" for m in sent)
        first = dict(engine.scenario_error)
        engine.check_scenario_radio(report(ue))
        assert engine.scenario_error == first
        with pytest.raises(ValueError, match="scenario_invalid"):
            await engine.start_scenario(CONTEXT)
        response = await engine.start_video(ue, dict(client_seq=1))
        assert response["reason"] == "scenario_invalid"
        engine.send_scenario_context(ue)
        assert sent[-1]["type"] == "scenario_abort"
        engine.receive_descriptor(SimpleNamespace(), 0)
        engine.decoded(SimpleNamespace(), None, 0)
        # Physical GPU work must settle before the next attempt can arm.
        video.state = "FAILED"
        engine.inflight[("v", 0)] = {}
        assert not engine.idle()
        engine.inflight.clear()
        video.tracker_busy = True
        assert not engine.idle()
        video.tracker_busy = False
        engine.disarm_scenario(CONTEXT)
        engine.arm_scenario(dict(CONTEXT, attempt_id="retry"))
        assert engine.scenario_error is None
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["source", "missing", "connection", "protocol", "timeout"])
def test_association_coverage_failures_abort(armed, failure):
    engine, ue, _ = armed
    r = report(ue)
    if failure == "source":
        r["source_generation"] = "edge-b"
    elif failure == "missing":
        r["ues"] = {}
    elif failure == "connection":
        r["ues"][ue.uid]["connection_id"] = "old"
    elif failure == "protocol":
        del r["association_losses"]
    else:
        engine.scenario_report_at = time.monotonic() - 100
        engine.check_scenario_report_timeout()
    engine.check_scenario_radio(r)
    assert engine.scenario_error is not None


def test_unassigned_loss_does_not_abort(armed):
    engine, ue, _ = armed
    r = report(ue)
    r["association_losses"] = {"other": dict(reason="missing")}
    engine.check_scenario_radio(r)
    assert engine.scenario_error is None


def test_abort_notice_sent_once_per_connection_and_only_to_participants(armed):
    engine, ue, sent = armed
    engine.invalidate_scenario("association_lost")
    count = lambda: sum(m["type"] == "scenario_abort" for m in sent)
    assert count() == 1
    for _ in range(3):
        engine.send_scenario_context(ue)
    assert count() == 1

    # A reconnect must receive the abort even though the old socket did.
    ue.generation += 1
    ue.connected = False
    engine.send_scenario_context(ue)
    assert count() == 1
    ue.connected = True
    engine.send_scenario_context(ue)
    assert count() == 2
    ue.alias = "not-in-this-scenario"
    engine.send_scenario_context(ue)
    assert count() == 2


def test_linux_abort_stops_media_without_disconnecting_control():
    client = Client.__new__(Client)
    client.collection_mode = True
    for key, value in CONTEXT.items():
        setattr(client, key, value)
    stopped = []
    client.streamer = SimpleNamespace(stop=lambda: stopped.append(True))
    client.record = lambda *args, **kwargs: None
    client.abort_collection(dict(CONTEXT, attempt_id="old"))
    assert not stopped
    client.abort_collection(CONTEXT)
    assert stopped == [True]
    assert client.streamer is None and client.assignment is None
    assert client.aborted_attempt_id == CONTEXT["attempt_id"]


@pytest.mark.parametrize("failure", [None, "loss", "timeout", "source", "context", "stale", "during_rpc"])
def test_finalization_confirms_retained_history_before_accepting(armed, tmp_path, failure):
    async def exercise():
        engine, ue, _ = armed
        engine.scenario_start_unix_ns = time.time_ns() - 1_000_000_000
        engine.scenario_videos = {ue.alias: "v"}
        engine.videos["v"] = Video("v", ue.uid, "detection", 1, 0, state="COMPLETED")
        root = Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "config/edgeric.shadow.yaml").read_text())
        cfg["runs_dir"] = str(tmp_path / "edge")
        edge = Runtime(cfg)
        sync = dict(
            schema_version=1, type="sync", run_id=engine.cfg.run_id,
            awareness_generation=engine.awareness_generation, awareness_revision=1,
            associations=[dict(ue_id=ue.uid, connection_id=ue.connection_id, ip=ue.ip)],
            sessions=[], **CONTEXT,
        )
        await edge.exchange(sync)
        engine.scenario_report_source = edge.source
        b = edge.associations.bindings[ue.uid]
        b.rnti = 100
        b.revalidate_at = b.valid_until = time.monotonic() + 100
        edge.metrics = {100: {}}
        edge.latest_metric = time.monotonic()
        if failure == "loss":
            # Loss and recovery both occur before the next periodic report.
            edge.associations.invalidate(ue.uid, "brief_loss")
            b.rnti = 100
            b.revalidate_at = b.valid_until = time.monotonic() + 100
        elif failure == "source":
            engine.scenario_report_source = "retired-source"
        elif failure == "stale":
            edge.latest_metric -= 10
        calls = []

        async def call(message, timeout):
            calls.append(message)
            if failure == "timeout":
                raise TimeoutError("RPC timeout")
            result = await edge.exchange(message)
            if failure == "context":
                return dict(result, attempt_id="wrong")
            if failure == "during_rpc":
                engine.invalidate_scenario("association_lost")
            return result

        engine.rpc = SimpleNamespace(call=call)
        if failure:
            with pytest.raises(ValueError, match="scenario finalization failed"):
                await engine.finalize_scenario(CONTEXT)
            assert engine.scenario_error
            assert engine.scenario_finalization is None
        else:
            result = await engine.finalize_scenario(CONTEXT)
            assert result["status"] == "finalized"
            assert result["association_snapshot"]["association_losses"] == {}
            # Repeated calls are cheap and do not move the completion boundary.
            assert await engine.finalize_scenario(CONTEXT) == result
            assert len(calls) == 1
            edge.associations.invalidate(ue.uid, "after_completion")
            assert await edge.exchange(calls[0]) == result["association_snapshot"]
            engine.check_scenario_radio(dict(report(ue), association_losses={ue.uid: {}}))
            assert engine.scenario_error is None
            await edge.exchange(dict(sync, attempt_id="retry", awareness_revision=2))
            assert edge.association_finalization is None
            with pytest.raises(ValueError, match="scenario context mismatch"):
                await edge.exchange(calls[0])
    asyncio.run(exercise())


def test_finalize_requires_drained_work_and_matching_attempt(armed):
    async def exercise():
        engine, _, _ = armed
        with pytest.raises(ValueError, match="started and drained"):
            await engine.finalize_scenario(CONTEXT)
        with pytest.raises(ValueError, match="scenario_context_mismatch"):
            await engine.finalize_scenario(dict(CONTEXT, attempt_id="old"))
    asyncio.run(exercise())


def test_finalize_api_returns_conflict_for_unfinished_attempt(armed):
    from fastapi.testclient import TestClient
    from mec_server.app import create_app

    engine, _, _ = armed
    # No lifespan: exercise HTTP validation without starting GPU/UDP services.
    client = TestClient(create_app(engine))
    response = client.post("/api/scenario/finalize", json=CONTEXT)
    assert response.status_code == 409
    assert "started and drained" in response.json()["error"]
    client.close()
