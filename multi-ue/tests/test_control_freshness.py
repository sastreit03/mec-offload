import asyncio
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mec_server.app import create_app
from mec_server.config import load
from mec_server.engine import Engine


@pytest.fixture
def engine(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Keep this mock fixture independent of the live campaign's catalog FPS.
    settings = yaml.safe_load((root / "config/mec.development.yaml").read_text())
    catalog_data = yaml.safe_load((root / "config/tasks.yaml").read_text())
    settings.update({key: catalog_data[key] for key in ("fps", "gop_frames")})
    for key in ("catalog", "profiles"):
        settings[key] = str(root / "config" / settings[key])
    config_path = tmp_path / "mec.yaml"
    config_path.write_text(yaml.safe_dump(settings))
    cfg, catalog, profiles = load(config_path)
    cfg.runs_dir = str(tmp_path)
    cfg.control_port = -1  # Probe receiver binds ephemeral UDP port 0.
    result = Engine(cfg, catalog, profiles)
    yield result
    result.record.close()


@pytest.mark.parametrize("mode,timeout", [("collection", 5), ("development", 30)])
def test_idle_timeout_and_incoming_message_refresh(engine, mode, timeout):
    engine.cfg.mode = mode
    ue, _ = engine.hello(None, "12.1.1.4", lambda _: None)
    assert engine.scenario is None
    assert engine.control_timeout_s == timeout
    ue.last_control_at = time.monotonic() - timeout + 1
    assert not engine.expire_control(ue.uid, ue.generation)
    asyncio.run(engine.command(ue, dict(type="time_sync", client_mono_ns=1)))
    assert engine.control_age_s(ue) < 1
    assert not engine.expire_control(ue.uid, ue.generation)
    ue.last_control_at = time.monotonic() - timeout - 1
    assert engine.expire_control(ue.uid, ue.generation)
    assert not ue.connected and ue.sender is None
    disconnected_at = ue.disconnect_at
    engine.disconnect(ue.uid, ue.generation)
    assert ue.disconnect_at == disconnected_at


def test_old_connection_cannot_expire_or_disconnect_reconnected_ue(engine):
    ue, hello = engine.hello(None, "12.1.1.4", lambda _: None)
    old_generation = ue.generation
    ue.last_control_at = time.monotonic() - 31
    assert engine.expire_control(ue.uid, old_generation)
    resumed, _ = engine.hello(hello["resume_token"], "12.1.1.4", lambda _: None)
    assert resumed is ue and ue.connected
    assert engine.control_age_s(ue) < 1
    assert not engine.expire_control(ue.uid, old_generation)
    engine.disconnect(ue.uid, old_generation)
    assert ue.connected


def test_idle_socket_closes_and_status_explains_timeout(engine):
    with TestClient(create_app(engine)) as client:
        with client.websocket_connect("/ws/ue") as socket:
            hello = socket.receive_json()
            uid = hello["ue_id"]
            status = client.get("/api/status").json()["ues"][uid]
            assert status["connected"]
            assert status["last_control_age_s"] < 5
            assert status["control_timeout_s"] == 30
            # Age the connection without waiting thirty seconds in the test.
            engine.ues[uid].last_control_at = time.monotonic() - 31
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1001
            status = client.get("/api/status").json()["ues"][uid]
            assert not status["connected"]
            assert status["last_control_age_s"] >= 31
