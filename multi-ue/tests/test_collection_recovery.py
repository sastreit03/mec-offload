import asyncio
import time
from types import SimpleNamespace

import pytest

from mec_server.engine import Video
from mec_ue.client import Client
from test_collection_abort import armed, CONTEXT
from muApp_mec.association import Associations


@pytest.mark.parametrize("elapsed,ip,invalid", [(.5, "12.1.1.3", False), (1.1, "12.1.1.3", True), (.1, "12.1.1.4", True)])
def test_reconnect_requires_same_ip_within_one_second(armed, monkeypatch, elapsed, ip, invalid):
    engine, _, _ = armed
    messages = []
    u, hello = engine.hello(None, "12.1.1.3", messages.append)
    u.alias = "linux1"
    u.ip = "12.1.1.3"
    engine.scenario_assignments[u.alias] = {}
    old_id, old_generation = u.connection_id, u.generation
    engine.disconnect(u.uid, u.generation)
    monkeypatch.setattr("mec_server.engine.time.monotonic", lambda: u.disconnect_at + elapsed if u.disconnect_at else 100)
    engine.hello(hello["resume_token"], ip, messages.append)
    assert bool(engine.scenario_error) is invalid
    assert (u.connection_id == old_id) is not invalid
    assert u.generation == old_generation + 1


def test_disconnect_grace_and_half_open_control_timeout(armed):
    engine, u, _ = armed
    engine.scenario_start_unix_ns = 1
    engine.disconnect(u.uid, u.generation)
    engine.check_scenario_control()
    assert engine.scenario_error is None
    u.disconnect_at -= 1.1
    engine.check_scenario_control()
    assert engine.scenario_error["reason"] == "control_disconnect_timeout"


def test_unresponsive_control_uses_existing_traffic(armed):
    engine, u, _ = armed
    engine.scenario_start_unix_ns = 1
    u.last_control_at = time.monotonic() - 2.1
    engine.check_scenario_control()
    assert engine.scenario_error is None
    u.last_control_at = time.monotonic() - 3.1
    engine.check_scenario_control()
    assert engine.scenario_error["reason"] == "control_unreachable"


@pytest.mark.parametrize("stopping", [False, True])
def test_per_ue_media_timeout_is_disabled_after_normal_stop(armed, stopping):
    engine, u, _ = armed
    engine.scenario_start_unix_ns = 1
    engine.scenario_stop_sent = stopping
    v = Video("v", u.uid, "detection", 1, 0, state="ACTIVE",
              last_complete_ns=time.monotonic_ns() - 1_100_000_000)
    engine.videos[v.vid] = v
    engine.scenario_videos[u.alias] = v.vid
    engine.check_scenario_media_progress()
    assert bool(engine.scenario_error) is not stopping
    if not stopping:
        assert engine.scenario_error["reason"] == "media_arrival_timeout"


def test_linux_local_timeout_requires_new_attempt():
    async def exercise():
        c = Client.__new__(Client)
        c.collection_mode = True
        for k, v in CONTEXT.items(): setattr(c, k, v)
        c.aborted_attempt_id = None
        c.assignment = {"anything": True}
        c.control_disconnected_at = time.monotonic() - 1.1
        c.last_control_at = time.monotonic()
        c.stopped = False
        c.record = lambda *a, **k: None
        stops = []
        c.streamer = SimpleNamespace(stop=lambda: stops.append(True))
        task = asyncio.create_task(c.control_watchdog())
        await asyncio.sleep(.01)
        c.stopped = True
        await task
        assert stops == [True]
        assert c.aborted_attempt_id == CONTEXT["attempt_id"]
        assert c.pending_failure["reason"] == "control_disconnect_timeout"
        assert c.pending_failure["attempt_id"] == CONTEXT["attempt_id"]
    asyncio.run(exercise())


def test_brief_socket_replacement_preserves_binding_and_fences_old_updates(armed):
    engine, _, _ = armed
    u, hello = engine.hello(None, "12.1.1.8", lambda _: None)
    u.alias = "linux1"
    u.ip = "12.1.1.8"
    engine.scenario_assignments[u.alias] = {}
    old_id, old_generation = u.connection_id, u.generation
    # New socket can arrive before old socket's disconnect callback.
    engine.hello(hello["resume_token"], "12.1.1.8", lambda _: None)
    assert u.connection_id == old_id
    engine.disconnect(u.uid, old_generation)
    assert u.connected
    losses = []
    associations = Associations({}, on_loss=lambda *args: losses.append(args))
    binding = associations.register(u.uid, old_id, u.ip, connection_generation=old_generation)
    binding.rnti = 100
    associations.register(u.uid, old_id, u.ip, connection_generation=u.generation)
    assert binding.rnti == 100 and not losses
    associations.register(u.uid, "obsolete", "12.1.1.9", connection_generation=old_generation)
    assert associations.bindings[u.uid] is binding
    assert binding.ip == "12.1.1.8"
