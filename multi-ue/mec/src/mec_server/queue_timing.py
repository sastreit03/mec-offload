"""Opt-in CPython 3.12 input-queue diagnostics, preserving the pickle/pipe path.

The feeder lifecycle/error handling follows multiprocessing.queues (PSF license).
Private queue internals are deliberately confined here; the default queue is
unchanged. Wall minus thread CPU time is NOT a scheduler-delay measurement.
"""
import errno
import multiprocessing.queues as mq
from multiprocessing.util import Finalize, is_exiting
import sys
import threading
import time
import weakref


def stamp(times, name):
    times[name + "_ns"] = time.monotonic_ns()
    times[name + "_thread_cpu_ns"] = time.thread_time_ns()


class TimedInputQueue(mq.Queue):
    def __init__(self, *, ctx):
        if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
            raise RuntimeError("MEC_TRACE_HANDOFF queue instrumentation requires CPython 3.12")
        super().__init__(ctx=ctx)

    def _reset(self, after_fork=False):
        super()._reset(after_fork)
        self.trace_lock = threading.Lock()
        self.traces = {}

    def take_trace(self, job_id):
        with self.trace_lock:
            return self.traces.pop(tuple(job_id), {})

    def get(self, block=True, timeout=None):
        # The worker uses this exact blocking form. Preserve other Queue APIs.
        if not block or timeout is not None:
            return super().get(block, timeout)
        if self._closed:
            raise ValueError("Queue is closed")
        times = {}
        stamp(times, "ipc_get_enter")
        with self._rlock:
            stamp(times, "ipc_read_begin")
            data = self._recv_bytes()
            stamp(times, "ipc_read_end")
        self._sem.release()
        stamp(times, "ipc_deserialize_begin")
        job = mq._ForkingPickler.loads(data)
        stamp(times, "ipc_deserialize_end")
        if job is not None:
            job.setdefault("handoff_times", {}).update(times)
        return job

    def _start_thread(self):
        self._buffer.clear()
        self._thread = threading.Thread(
            target=self._timed_feed,
            args=(self._buffer, self._notempty, self._send_bytes, self._wlock,
                  self._reader.close, self._writer.close, self._ignore_epipe,
                  self._on_queue_feeder_error, self._sem, self.traces, self.trace_lock),
            name="TimedQueueFeederThread", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            raise
        if not self._joincancelled:
            self._jointhread = Finalize(self._thread, mq.Queue._finalize_join,
                                       [weakref.ref(self._thread)], exitpriority=-5)
        self._close = Finalize(self, mq.Queue._finalize_close,
                              [self._buffer, self._notempty], exitpriority=10)

    @staticmethod
    def _timed_feed(buffer, notempty, send_bytes, writelock, reader_close,
                    writer_close, ignore_epipe, onerror, queue_sem, traces, lock):
        while True:
            try:
                with notempty:
                    if not buffer:
                        notempty.wait()
                try:
                    while True:
                        obj = buffer.popleft()
                        if obj is mq._sentinel:
                            reader_close()
                            writer_close()
                            return
                        key = tuple(obj["job_id"]) if isinstance(obj, dict) else None
                        times = {}
                        stamp(times, "ipc_serialize_begin")
                        data = mq._ForkingPickler.dumps(obj)
                        stamp(times, "ipc_serialize_end")
                        times["ipc_serialized_bytes"] = len(data)
                        stamp(times, "ipc_write_lock_begin")
                        if writelock is not None:
                            writelock.acquire()
                        try:
                            stamp(times, "ipc_write_begin")
                            send_bytes(data)
                            stamp(times, "ipc_write_end")
                        finally:
                            if writelock is not None:
                                writelock.release()
                        if key is not None:
                            with lock:
                                traces[key] = times
                except IndexError:
                    pass
            except Exception as error:
                if ignore_epipe and getattr(error, "errno", 0) == errno.EPIPE:
                    return
                if is_exiting():
                    return
                queue_sem.release()
                onerror(error, obj)
