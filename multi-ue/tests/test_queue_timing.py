"""Timing hooks preserve real spawned-process queue transfer and result identity."""
import multiprocessing as mp
import time

import numpy as np

from mec_server.queue_timing import TimedInputQueue
from mec_server.workers import Workers


def receive_one(incoming, outgoing):
    job = incoming.get()
    outgoing.put((job["job_id"], job["rgb"], job["handoff_times"]))
    assert incoming.get() is None


def test_timed_queue_preserves_rgb_and_sentinel():
    ctx = mp.get_context("spawn")
    incoming = TimedInputQueue(ctx=ctx)
    outgoing = ctx.Queue()
    child = ctx.Process(target=receive_one, args=(incoming, outgoing))
    rgb = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
    child.start()
    try:
        incoming.put(dict(job_id=["video", 7], rgb=rgb, handoff_times={"source": 1}))
        incoming.put(None)
        identity, received, times = outgoing.get(timeout=10)
        assert identity == ["video", 7]
        np.testing.assert_array_equal(received, rgb)
        assert times["source"] == 1
        assert times["ipc_get_enter_ns"] <= times["ipc_read_begin_ns"] <= times["ipc_read_end_ns"]
        assert times["ipc_read_end_ns"] <= times["ipc_deserialize_begin_ns"] <= times["ipc_deserialize_end_ns"]
        child.join(10)
        assert child.exitcode == 0
        trace = incoming.take_trace(identity)
        assert trace["ipc_serialize_begin_ns"] <= trace["ipc_serialize_end_ns"] <= trace["ipc_write_begin_ns"] <= trace["ipc_write_end_ns"]
        assert trace["ipc_serialized_bytes"] > rgb.nbytes
    finally:
        if child.is_alive():
            child.terminate()
            child.join()
        incoming.close()
        incoming.join_thread()
        outgoing.close()


def test_worker_returns_correlated_queue_timings(monkeypatch):
    monkeypatch.setenv("MEC_TRACE_HANDOFF", "1")
    candidates = [dict(application="detection", model=str(n)) for n in range(4)]
    workers = Workers(dict(configurations=candidates),
                      dict(backend="mock", applications=["detection"], priority_streams=[0]))
    workers.start()
    deadline = time.monotonic() + 15
    try:
        while not workers.ready:
            list(workers.poll())
            assert time.monotonic() < deadline
            time.sleep(.001)
        assert workers.submit("detection", 0, dict(job_id=["v", 3], priority=0,
                              configuration=candidates[0], rgb=np.zeros((8, 8, 3), np.uint8)))
        while True:
            results = list(workers.poll())
            if results:
                break
            assert time.monotonic() < deadline
            time.sleep(.001)
        result, = results
        assert result["type"] == "completed" and result["job_id"] == ["v", 3]
        times = result["times"]
        assert times["ipc_sender_trace_available"]
        assert times["ipc_put_begin_ns"] <= times["ipc_serialize_begin_ns"]
        assert times["ipc_serialize_end_ns"] <= times["ipc_deserialize_begin_ns"]
        assert times["worker_get_begin_ns"] <= times["ipc_read_begin_ns"]
        assert times["ipc_deserialize_end_ns"] <= times["worker_get_end_ns"]
    finally:
        workers.close()
