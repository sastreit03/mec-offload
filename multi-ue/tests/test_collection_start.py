import asyncio
import json
import threading
import time
import sys
import uuid
from types import SimpleNamespace

import pytest

from mec_server.control import SendReceipt
from mec_server.engine import Video
from mec_server.app import create_app
from mec_ue.client import Client
from test_collection_abort import armed, CONTEXT


def prepare(engine, user):
    video = Video(user.uid, user.uid, "detection", 1, 0, state="AWAITING_DELIVERY")
    engine.videos[video.vid] = video
    engine.scenario_assignments[user.alias] = {}
    engine.scenario_videos[user.alias] = video.vid
    user.scenario_ack = (CONTEXT["attempt_id"], user.generation, user.alias)
    return video


@pytest.mark.parametrize("mode", ["success", "queued_retry", "writing", "timeout", "reconnect", "abort"])
def test_queue_all_before_wait_with_one_retry_stage(armed, mode):
    async def exercise():
        engine, user, _ = armed
        other, _ = engine.hello(None, "12.1.1.3", lambda _: None)
        other.alias = "linux1"
        prepare(engine, user)
        prepare(engine, other)
        receipts = {user.uid: [], other.uid: []}
        order = []
        loop = asyncio.get_running_loop()

        def enqueue(uid, message):
            if message["type"] != "scenario_start":
                return
            r = SendReceipt()
            receipts[uid].append(r)
            order.append(uid)
            assert "release_delay_ns" not in message
            if uid == other.uid or mode == "success" or (mode == "queued_retry" and len(receipts[uid]) == 2):
                loop.call_soon(r.finish)
            elif mode == "writing":
                r.started_ns = time.monotonic_ns()
                loop.call_later(0.25, r.finish)
            elif mode == "reconnect":
                loop.call_soon(setattr, user, "generation", user.generation + 1)
            elif mode == "abort":
                loop.call_soon(engine.invalidate_scenario, "association_lost")
            return r

        user.sender = lambda m: enqueue(user.uid, m)
        other.sender = lambda m: enqueue(other.uid, m)
        if mode in ("timeout", "reconnect", "abort"):
            with pytest.raises(ValueError):
                await engine.start_scenario(CONTEXT)
            assert engine.scenario_error
        else:
            result = await engine.start_scenario(CONTEXT)
            assert result["status"] == "started"
            assert result["queue_loop_ns"] >= 0
            assert await engine.start_scenario(CONTEXT) == result
        assert order[:2] == [user.uid, other.uid]
        assert len(receipts[other.uid]) == 1
        assert len(receipts[user.uid]) == (2 if mode in ("queued_retry", "timeout") else 1)
        if mode in ("queued_retry", "timeout"):
            assert receipts[user.uid][0].done.cancelled()
        assert all(r.done.done() for items in receipts.values() for r in items)
    asyncio.run(exercise())


def test_readiness_ack_requires_admitted_matching_video(armed):
    async def exercise():
        engine, user, _ = armed
        with pytest.raises(ValueError, match="not prepared"):
            await engine.command(user, dict(CONTEXT, type="scenario_context_ack"))
        video = prepare(engine, user)
        user.scenario_ack = None
        with pytest.raises(ValueError, match="not ready"):
            await engine.start_scenario(CONTEXT)
        await engine.command(user, dict(CONTEXT, type="scenario_context_ack", video_id=video.vid))
        assert user.scenario_ack == (CONTEXT["attempt_id"], user.generation, user.alias)
    asyncio.run(exercise())


def test_websocket_sender_tracks_completion_and_skips_withdrawn_start(armed):
    async def exercise():
        engine, _, _ = armed
        queue = asyncio.Queue()
        sent = []
        class Socket:
            headers = {}
            client = SimpleNamespace(host="12.1.1.4")
            async def accept(self): pass
            async def send_text(self, value): sent.append(json.loads(value))
            async def receive_text(self): return await queue.get()
            async def close(self, **kwargs): pass
        endpoint = next(r.endpoint for r in create_app(engine).routes if r.path == "/ws/ue")
        task = asyncio.create_task(endpoint(Socket()))
        await asyncio.sleep(0)
        user = engine.ues[sent[0]["ue_id"]]
        message = dict(CONTEXT, type="scenario_start", run_id=engine.cfg.run_id)
        withdrawn = user.sender(message)
        withdrawn.done.cancel()
        receipt = user.sender(message)
        assert await asyncio.wait_for(receipt.done, 1) is None
        assert receipt.started_ns is not None and receipt.finished_ns >= receipt.started_ns
        assert sum(m["type"] == "scenario_start" for m in sent) == 1
        # A queued start cannot escape after an abort invalidates the attempt.
        late = user.sender(message)
        engine.scenario_error = {"reason": "test"}
        assert await asyncio.wait_for(late.done, 1) == "obsolete_start"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_linux_ready_ack_and_release_are_separate_and_idempotent():
    async def exercise():
        client = Client.__new__(Client)
        for key, value in CONTEXT.items(): setattr(client, key, value)
        client.collection_mode = True
        client.aborted_attempt_id = client.started_attempt_id = None
        client.record = lambda *a, **k: None
        releases, messages = [], []
        client.streamer = SimpleNamespace(prepared=True, response={"video_id": "v"}, release=lambda: releases.append(True))
        async def send(value): messages.append(value)
        client.send = send
        await client.on_media("media_prepared", {"video_id": "v"})
        assert messages == [{"type": "scenario_context_ack", "video_id": "v"}]
        assert not releases
        client.release_collection(dict(CONTEXT, attempt_id="old"))
        assert not releases
        client.release_collection(CONTEXT)
        client.release_collection(CONTEXT)
        assert releases == [True]
        assert len(messages) == 1  # No application-layer start acknowledgment.
    asyncio.run(exercise())


@pytest.mark.parametrize("abort", [False, True])
def test_linux_media_holds_first_frame_until_release_and_stop_wakes_it(monkeypatch, abort):
    from mec_ue import media
    prepared = threading.Event()
    reads, encoded, packets, closed, events = [], [], [], [], []
    cap = SimpleNamespace(
        isOpened=lambda: True,
        read=lambda: (reads.append("first") is None, "first-frame"),
        release=lambda: closed.append("capture"),
    )
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        VideoCapture=lambda _: cap, resize=lambda frame, _: frame,
    ))
    class Encoder:
        def __init__(self, *args): pass
        def frame(self, frame, *args, **kwargs):
            encoded.append(frame)
            return [b"\x65test"]
        def close(self): closed.append("encoder")
    sock = SimpleNamespace(
        bind=lambda _: None, connect=lambda _: None,
        getsockname=lambda: ("12.1.1.2", 0), send=packets.append,
        close=lambda: closed.append("socket"),
    )
    monkeypatch.setattr(media, "Encoder", Encoder)
    monkeypatch.setattr(media, "socket", SimpleNamespace(
        AF_INET=2, SOCK_DGRAM=2, socket=lambda *_: sock,
    ))
    response = dict(
        video_id=str(uuid.uuid4()), media_epoch=1, endpoint=dict(host="mec", port=1),
        rtp=dict(mtu=1200), config=dict(
            width=10, height=10, fps=30, bitrate_bps=500000, gop_frames=30,
            config_version=1, plan_version=1, boundary_unix_ns=0,
        ),
    )
    clock = SimpleNamespace(unix_ns=time.time_ns, valid=lambda: True, to_unix=lambda _: time.time_ns())
    def event(kind, value):
        events.append(kind)
        if kind == "media_prepared": prepared.set()
    streamer = media.Streamer(response, "clip.mp4", "12.1.1.2", clock, event,
                              frame_count=1, hold_for_start=True)
    streamer.start()
    try:
        assert prepared.wait(1)
        assert reads == ["first"] and not encoded and not packets
        if abort:
            streamer.stop()
        else:
            streamer.release()
        streamer.thread.join(1)
        assert not streamer.thread.is_alive()
        assert "sender_error" not in events
        assert encoded == ([] if abort else ["first-frame"])
        assert bool(packets) is not abort
        assert set(closed) == {"encoder", "socket", "capture"}
    finally:
        streamer.stop()
        streamer.thread.join(1)
