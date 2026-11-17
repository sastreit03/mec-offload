import asyncio
import threading
import time

import pytest

from test_control_freshness import engine


@pytest.mark.parametrize("outcome", ["ready", "failure", "aborted"])
def test_tracker_setup_keeps_control_loop_responsive(engine, monkeypatch, outcome):
    async def exercise():
        loop_thread = threading.get_ident()
        entered = threading.Event()
        release = threading.Event()
        sentinel = object()

        def initialize(*args):
            assert threading.get_ident() != loop_thread
            entered.set()
            if not release.wait(2):
                raise RuntimeError("control loop did not advance")
            if outcome == "failure":
                raise RuntimeError("tracker initialization failed")
            return sentinel

        monkeypatch.setattr("mec_server.engine.Tracker", initialize)
        monkeypatch.setattr(engine, "readiness", lambda: {"ready": True})
        monkeypatch.setattr(engine, "usable_radio", lambda *a, **k: True)
        user, _ = engine.hello(None, "12.1.1.3", lambda _: None)
        task = asyncio.create_task(engine.start_video(user, dict(
            client_seq=1, application="tracking",
            input=dict(width=1280, height=720, fps=engine.cfg.fps),
        )))
        deadline = time.monotonic() + 2
        while not entered.is_set() and not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        try:
            assert entered.is_set()
            # Control commands must run while initialization is still blocked.
            reply = await engine.command(user, dict(type="time_sync", client_mono_ns=1))
            assert reply["type"] == "time_sync"
            assert not task.done()
            if outcome == "aborted":
                engine.cfg.mode = "collection"
                engine.scenario_error = {"reason": "operator_abort"}
        finally:
            release.set()
        result = await task
        assert result["accepted"] is (outcome == "ready")
        video = engine.videos[result["video_id"]]
        if outcome != "ready":
            assert result["reason"] == (
                "setup_failed" if outcome == "failure" else "scenario_invalid"
            )
            assert video.state == "DENIED" and user.active_video is None
        else:
            assert video.tracker is sentinel
            video.receiver.close()

    asyncio.run(exercise())
