"""Receiver/IPC acceptance and an opt-in test of the actual CUDA launcher."""
import asyncio
import os
import queue
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import numpy as np
import pytest

from mec_server.handoff import ReceiverHandoff
from mec_server import workers


def test_fifo_without_tick_and_independent_receiver_limits():
    async def exercise():
        loop = asyncio.get_running_loop()
        seen, losses = [], []

        def overflow(count, descriptors):
            losses.append((count, [d.frame_id for d in descriptors]))

        a = ReceiverHandoff(loop, lambda kind, data: seen.append(("a", kind)), overflow)
        b = ReceiverHandoff(loop, lambda kind, data: seen.append(("b", kind)), overflow)
        d = SimpleNamespace(frame_id=4)

        def producer():
            a("descriptor", {})
            for _ in range(1013):
                a("progress", {})
            for _ in range(10):
                a("decoded", {})
            a("progress", {"descriptor": d})
            b("descriptor", {})
            b("progress", {})
            for _ in range(10):
                b("decoded", {})
            b("decoded", {"descriptor": d})

        thread = Thread(target=producer)
        thread.start()
        thread.join()
        assert a.stats()["depth"] == 1024
        assert b.stats()["depth"] == 12
        async with asyncio.timeout(2):
            while len(seen) < 1036:
                await asyncio.sleep(0)
        assert [k for r, k in seen if r == "a"] == ["descriptor"] + ["progress"] * 1013 + ["decoded"] * 10
        assert [k for r, k in seen if r == "b"] == ["descriptor", "progress"] + ["decoded"] * 10
        assert losses == [(1, [4]), (1, [4])]
        assert a.stats()["peak_decoded"] == b.stats()["peak_decoded"] == 10
        assert not a.scheduled and not b.scheduled

    asyncio.run(exercise())


def test_same_priority_burst_through_spawned_worker():
    candidates = [dict(application="detection", model=str(n)) for n in range(4)]
    w = workers.Workers(dict(configurations=candidates),
                        dict(backend="mock", applications=["detection"], priority_streams=[0]))
    w.start()
    end = time.monotonic() + 20
    try:
        while not w.ready:
            list(w.poll())
            assert time.monotonic() < end
            time.sleep(.001)
        for n in range(12):
            assert w.submit("detection", 0, dict(job_id=["v", n], priority=0,
                            configuration=candidates[0], rgb=np.zeros((8, 8, 3), np.uint8)))
        ids = []
        while len(ids) < 12:
            for msg in w.poll():
                assert msg["type"] == "completed"
                ids.append(tuple(msg["job_id"]))
            assert time.monotonic() < end
            time.sleep(.001)
        assert sorted(ids) == [("v", n) for n in range(12)]
    finally:
        w.close()


def test_launcher_advances_while_first_result_is_unready(monkeypatch):
    from threading import Event

    release_first = Event()
    first_completed = Event()
    order = []

    class Adapter:
        def __init__(self, *args, **kwargs):
            pass

        def launch(self, job):
            fid = job["job_id"][1]
            order.append(fid)
            if fid == 1:
                assert not first_completed.is_set()
                release_first.set()
            return SimpleNamespace(ready=SimpleNamespace(query=release_first.is_set), times={})

        def finish(self, job, submission):
            assert release_first.is_set()
            if job["job_id"][1] == 0:
                first_completed.set()
            return {}, submission.times

    monkeypatch.setattr(workers, "TorchAdapter", Adapter)
    incoming, outgoing = queue.Queue(), queue.Queue()
    for fid in (0, 1):
        incoming.put(dict(job_id=["v", fid], priority=0, rgb=np.zeros((1, 1, 3), np.uint8)))
    incoming.put(None)
    workers.worker_main("detection", {}, dict(backend="torchscript", weights_dir="",
                        priority_streams=[0], warmup_count=0), incoming, outgoing)
    assert order == [0, 1]
    assert outgoing.get_nowait()["type"] == "ready"
    results = [outgoing.get_nowait(), outgoing.get_nowait()]
    assert all(m["type"] == "completed" for m in results)
    assert sorted(m["job_id"] for m in results) == [["v", 0], ["v", 1]]
    assert first_completed.is_set()


@pytest.mark.skipif(os.getenv("MEC_TEST_CUDA") != "1", reason="set MEC_TEST_CUDA=1 on the Spark")
def test_cuda_worker_launches_b_before_a_finishes(tmp_path, monkeypatch):
    import torch

    assert torch.cuda.is_available()

    class Boxes(torch.nn.Module):
        def forward(self, x):
            score = x.mean().float()
            return torch.stack([score * 0, score * 0, score, score, score, score * 0]).reshape(1, 1, 6)

    torch.jit.trace(Boxes(), torch.ones(1, 3, 32, 32)).save(str(tmp_path / "boxes.pt"))
    adapter_launch = workers.TorchAdapter.launch
    first_event = None
    overlap = []

    def instrumented_launch(self, job):
        nonlocal first_event
        fid = job.get("job_id")  # warmup uses the same synchronous adapter
        if fid == ["v", 0]:
            # Extend the first frame's stream residence for a deterministic probe.
            with torch.cuda.stream(self.streams[0]):
                torch.cuda._sleep(200_000_000)
        record = adapter_launch(self, job)
        if fid == ["v", 0]:
            first_event = record.ready
        elif fid == ["v", 1]:
            overlap.append(not first_event.query())
        return record

    monkeypatch.setattr(workers.TorchAdapter, "launch", instrumented_launch)
    spec = dict(application="detection", input_hw=[32, 32], artifact="boxes.pt", confidence=0)
    incoming, outgoing = queue.Queue(), queue.Queue()
    for n, value in enumerate([0, 255]):
        incoming.put(dict(job_id=["v", n], priority=0, configuration=dict(spec, model="tiny"),
                          rgb=np.full((32, 32, 3), value, dtype=np.uint8)))
    incoming.put(None)
    mps = Path(os.getenv("CUDA_MPS_PIPE_DIRECTORY", "/tmp/nvidia-mps")) / "control"
    workers.worker_main("detection", {"tiny": spec}, dict(backend="torchscript",
                        weights_dir=tmp_path, priority_streams=[0], warmup_count=3,
                        mps_enabled=mps.exists()), incoming, outgoing)
    assert (ready := outgoing.get_nowait())["type"] == "ready", ready
    messages = sorted([outgoing.get_nowait(), outgoing.get_nowait()], key=lambda m: m["job_id"])
    assert overlap == [True], "B finished launching after A's GPU work was already complete"
    for n, msg in enumerate(messages):
        assert msg["type"] == "completed", msg
        assert msg["job_id"] == ["v", n] and msg["priority"] == 0
        assert msg["payload"]["detections"][0]["score"] == pytest.approx(n)
        assert msg["worker_start_ns"] <= msg["times"]["gpu_launch_end_ns"] <= msg["worker_finish_ns"]
        if os.getenv("MEC_TRACE_HANDOFF") == "1":
            times = msg["times"]
            assert times["worker_get_begin_ns"] <= times["worker_get_end_ns"] <= times["launch_enter_ns"]
            previous = times["launch_enter_ns"]
            for stage in ("pin", "upload", "preprocess", "model_call", "output_ops", "output_alloc", "readback"):
                assert previous <= times[f"launch_{stage}_begin_ns"] <= times[f"launch_{stage}_end_ns"]
                previous = times[f"launch_{stage}_end_ns"]
            assert previous <= times["launch_exit_ns"] <= msg["worker_finish_ns"]
    assert outgoing.empty()
