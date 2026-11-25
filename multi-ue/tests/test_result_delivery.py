import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mec_server.engine import Engine
from mec_server.recording import ReplayStore
from mec_server.results import SerializedResult
from mec_contracts.wire import dumps


def message():
    return dict(type="frame_result", run_id="run", campaign_id="campaign",
                scenario_id="scenario", attempt_id="attempt", ue_id="ue",
                video_id="video", frame_id=1, result_id="result", outcome="completed",
                stage_times={"processing_s": .01}, payload={"detections": []})


@pytest.mark.parametrize("payload", [{"rle": [[1, 100], [2, 50]]}, None])
def test_payload_follows_all_receipt_fields_without_mutating_message(payload):
    original = {"payload": payload, **message(), "reason": None}
    original["payload"] = payload
    original_keys = list(original)
    result = SerializedResult.prepare(original)
    wire = json.loads(result.text)
    assert list(wire)[-1] == "payload"
    assert wire == {k: v for k, v in original.items() if k != "stage_times"}
    assert list(original) == original_keys
    assert "payload" not in result and "stage_times" not in result


def test_result_without_payload_does_not_add_one():
    original = message()
    del original["payload"]
    assert "payload" not in json.loads(SerializedResult.prepare(original).text)


def test_single_serialization_and_live_delivery_before_commit(tmp_path):
    async def exercise():
        store = ReplayStore(tmp_path / "replay.sqlite3")
        entered, release = threading.Event(), threading.Event()
        write = store._write
        def blocked(connection, values):
            entered.set()
            assert release.wait(5)
            write(connection, values)
        store._write = blocked
        records, sent, errors, milestones = [], [], [], []
        video = SimpleNamespace(uid="ue", vid="video", deliveries=1, collection_error=None)
        engine = SimpleNamespace(
            contextual=lambda m: m, replay=store, collection_mode=True,
            record=SimpleNamespace(put=lambda name, m: records.append((name, m)),
                                   event=lambda kind, **k: milestones.append((kind, k))),
            send=lambda uid, m: sent.append(m),
            invalidate_scenario=lambda *a, **k: errors.append((a, k)))
        task = None
        try:
            with patch("mec_server.results.dumps", wraps=dumps) as encode:
                task = asyncio.create_task(Engine.deliver(engine, video, message(), None, 0, None))
                for _ in range(200):
                    if sent and entered.is_set():
                        break
                    await asyncio.sleep(.005)
                assert sent and entered.is_set()
                assert not task.done() and video.deliveries == 1
                assert store.maximum("ue") == 0
                wire = json.loads(sent[0].text)
                assert "stage_times" not in wire
                assert wire["payload"] == {"detections": []}
                evidence = records[0][1]
                assert "payload" not in evidence
                assert evidence["stage_times"]["processing_s"] == .01
                assert evidence["stage_times"]["result_prepare_s"] >= 0
                barrier = store.barrier()
                assert not barrier.done()
                release.set()
                await task
                assert video.deliveries == 0 and not errors
                await asyncio.wrap_future(barrier)
                rows = store.ordered_page("ue", store.maximum("ue"), 0, 16)
                assert rows[0].text == sent[0].text
                assert rows[0]["attempt_id"] == "attempt"
                assert encode.call_count == 1
                persisted = next(v for kind, v in milestones if kind == "result_persisted")
                assert persisted["result_id"] == "result" and persisted["attempt_id"] == "attempt"
                timing = persisted["stage_times"]
                assert timing["replay_submitted_ns"] <= timing["replay_writer_start_ns"] <= timing["replay_commit_end_ns"]
                assert "serialization_s" in evidence["stage_times"]
        finally:
            release.set()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            store.close()
    asyncio.run(exercise())


def test_barrier_is_finite_and_close_drains_all_ues(tmp_path):
    store = ReplayStore(tmp_path / "replay.sqlite3")
    entered, release = threading.Event(), threading.Event()
    write = store._write
    def blocked(connection, values):
        if values[0] == "ue2":
            entered.set()
            assert release.wait(5)
        write(connection, values)
    store._write = blocked
    try:
        first = store.submit("ue", "v", 1, "r1", '{}')
        barrier = store.barrier()
        second = store.submit("ue2", "v", 1, "r2", '{}')
        assert entered.wait(5)
        barrier.result(timeout=1)
        assert first.done() and not second.done()
        assert store.pending_bytes == 2
        release.set()
    finally:
        release.set()
        store.close()
    assert second.done() and store.pending_bytes == 0
    assert store.maximum("ue2") > 0


def test_byte_limit_is_explicit(tmp_path):
    store = ReplayStore(tmp_path / "replay.sqlite3", max_pending_bytes=1)
    with pytest.raises(RuntimeError, match="byte limit"):
        store.submit("u", "v", 1, "r", '{}')
    with pytest.raises(RuntimeError, match="writer failed"):
        store.close()


def test_write_failure_invalidates_collection(tmp_path):
    async def exercise():
        store = ReplayStore(tmp_path / "replay.sqlite3")
        def fail(*args):
            raise OSError("disk failure")
        store._write = fail
        errors, sent = [], []
        video = SimpleNamespace(uid="ue", vid="video", deliveries=1, collection_error=None)
        engine = SimpleNamespace(
            contextual=lambda m: m, replay=store, collection_mode=True,
            record=SimpleNamespace(put=lambda *a: None, event=lambda *a, **k: None),
            send=lambda *a: sent.append(a),
            invalidate_scenario=lambda *a, **k: errors.append((a, k)))
        with pytest.raises(OSError, match="disk failure"):
            await Engine.deliver(engine, video, message(), None, 0, None)
        assert sent and errors and video.deliveries == 0
        assert video.collection_error == "result_delivery_failed"
        with pytest.raises(RuntimeError, match="writer failed"):
            store.close()
    asyncio.run(exercise())


def test_legacy_replay_removes_stage_times():
    result = SerializedResult.from_storage(dumps(message()))
    assert "stage_times" not in json.loads(result.text)
    assert json.loads(result.text)["payload"] == message()["payload"]


def test_websocket_replay_waits_for_pending_write_and_reuses_text(tmp_path):
    from fastapi import WebSocketDisconnect
    from mec_server.app import create_app

    async def exercise():
        store = ReplayStore(tmp_path / "replay.sqlite3")
        entered, release = threading.Event(), threading.Event()
        write = store._write
        def blocked(connection, values):
            entered.set()
            assert release.wait(5)
            write(connection, values)
        store._write = blocked
        result = SerializedResult.prepare(message())
        saved = store.submit("ue", "video", 1, "result", result.text)
        user = SimpleNamespace(uid="ue", generation=1, connected=True)
        engine = SimpleNamespace(
            replay=store, collection_mode=True, ues={"ue": user},
            cfg=SimpleNamespace(run_id="run"),
            record=SimpleNamespace(event=lambda *a, **k: None),
            expire_control=lambda u, g: False,
            disconnect=lambda *a: setattr(user, "connected", False))
        def hello(token, ip, enqueue):
            user.sender = enqueue
            return user, {"type": "mec_hello"}
        engine.hello = hello
        class Socket:
            headers = {}
            client = SimpleNamespace(host="127.0.0.1")
            def __init__(self):
                self.sent = []
                self.stop = asyncio.Event()
            async def accept(self): pass
            async def send_text(self, text): self.sent.append(text)
            async def receive_text(self):
                await self.stop.wait()
                raise WebSocketDisconnect()
            async def close(self, **kwargs): self.stop.set()
        ws = Socket()
        endpoint = next(r.endpoint for r in create_app(engine).routes if r.path == "/ws/ue")
        task = asyncio.create_task(endpoint(ws))
        try:
            await asyncio.sleep(.03)
            assert entered.is_set() and ws.sent == ['{"type":"mec_hello"}']
            # A pending replay barrier must not block the connection's live sender.
            user.sender(result)
            await asyncio.sleep(.03)
            assert ws.sent[-1] == result.text and not saved.done()
            release.set()
            await asyncio.wait_for(asyncio.wrap_future(saved), 2)
            for _ in range(100):
                if len(ws.sent) == 3:
                    break
                await asyncio.sleep(.005)
            assert ws.sent == ['{"type":"mec_hello"}', result.text, result.text]
        finally:
            release.set()
            ws.stop.set()
            await task
            store.close()
    asyncio.run(exercise())
