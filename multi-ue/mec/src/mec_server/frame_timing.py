"""Host timing only. Optional bounded GC overlap is correlation, not causation."""
import gc
import os
import time
from collections import deque


class GCTrace:
    def __init__(self):
        self.intervals = deque(maxlen=2048)
        self.active = None
        self.enabled = False

    def start(self):
        if not self.enabled and os.getenv('MEC_TRACE_GC') == '1':
            gc.callbacks.append(self.callback)
            self.enabled = True

    def callback(self, phase, info):
        now = time.monotonic_ns()
        if phase == 'start':
            self.active = now
        elif self.active is not None:
            self.intervals.append((self.active, now))
            self.active = None

    def overlap(self, start, end):
        if not self.enabled:
            return {}
        intervals = list(self.intervals)
        if self.active is not None:
            intervals.append((self.active, end))
        # A full ring may have lost intervals: omit duration rather than undercount.
        if len(self.intervals) == self.intervals.maxlen and start < self.intervals[0][0]:
            return {'gc_overlap_available': False}
        spans = [max(0, min(end, b)-max(start, a)) for a, b in intervals]
        return dict(gc_overlap_available=True, gc_overlap_s=sum(spans)/1e9,
                    gc_overlap_count=sum(v > 0 for v in spans))


gc_trace = GCTrace()


def timed_rle(mask, times):
    from .workers import rle
    begin, cpu = time.monotonic_ns(), time.thread_time_ns()
    runs = rle(mask)
    end = time.monotonic_ns()
    times.update(rle_start_ns=begin, rle_end_ns=end, rle_s=(end-begin)/1e9,
                 rle_thread_s=(time.thread_time_ns()-cpu)/1e9, rle_run_count=len(runs))
    return runs


def record_preprocess_submission(event, stream, times, worker_dequeued_ns=None):
    """Bracket the host enqueue of the existing preprocessing-start marker."""
    begin = time.monotonic_ns()
    event.record(stream)
    end = time.monotonic_ns()
    times.update(cuda_preprocess_submit_start_ns=begin,
                 cuda_preprocess_submit_end_ns=end,
                 cuda_preprocess_submit_call_s=(end-begin)/1e9)
    if worker_dequeued_ns is not None:
        times['worker_dequeue_to_cuda_submit_s'] = (begin-worker_dequeued_ns)/1e9


def observe_preprocess_start(event, times):
    """Bound marker readiness using host queries; never infer kernel start.

    Submission occurs within [submit_start, submit_end]. An unsuccessful
    query gives a conservative readiness lower bound at query *entry*; a
    successful query gives an upper bound at query *return*. Both clocks
    are host monotonic. No CUDA timestamp conversion or synchronization.
    """
    if ('cuda_preprocess_submit_start_ns' not in times
            or 'preprocess_marker_observed_ns' in times):
        return
    begin = time.monotonic_ns()
    ready = event.query()
    end = time.monotonic_ns()
    if not ready:
        times['preprocess_marker_last_not_ready_ns'] = begin
        return
    submitted = times['cuda_preprocess_submit_start_ns']
    submit_end = times['cuda_preprocess_submit_end_ns']
    lower = times.get('preprocess_marker_last_not_ready_ns', submitted)
    times.update(preprocess_marker_observed_ns=end,
                 cuda_submit_to_preprocess_marker_lower_s=max(0, lower-submit_end)/1e9,
                 cuda_submit_to_preprocess_marker_upper_s=(end-submitted)/1e9,
                 preprocess_marker_observation_span_s=(end-lower)/1e9)



def add_stream_wait_estimate(times, cuda_pipeline_s):
    """Subtract durations, never host and CUDA timestamps.

    The residual includes enqueue overhead and completion observation delay.
    Keep signed estimates: clamping would hide timer error or bad boundaries.
    No estimate for synchronous/offline calls without observer evidence.
    """
    times['cuda_pipeline_s'] = cuda_pipeline_s
    start = times.get('cuda_preprocess_submit_start_ns')
    observed = times.get('gpu_completion_observed_ns')
    if start is None or observed is None:
        return
    elapsed = (observed-start)/1e9
    times['cuda_submission_to_completion_observed_s'] = elapsed
    times['stream_waiting_est_s'] = elapsed-cuda_pipeline_s
