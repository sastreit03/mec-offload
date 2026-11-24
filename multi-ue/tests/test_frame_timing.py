import numpy as np
from mec_server.frame_timing import GCTrace, timed_rle


def test_rle_preserves_payload_and_counts_runs():
    times = {}
    assert timed_rle(np.array([[1, 1, 2], [2, 3, 3]]), times) == [[1, 2], [2, 2], [3, 2]]
    assert times['rle_run_count'] == 3
    assert times['rle_s'] >= 0 and times['rle_thread_s'] >= 0
    assert times['rle_start_ns'] <= times['rle_end_ns']


def test_gc_overlap_clipping_and_unavailable():
    trace = GCTrace()
    assert trace.overlap(10, 30) == {}
    trace.enabled = True
    trace.intervals.extend([(0, 15), (20, 25), (40, 50)])
    assert trace.overlap(10, 30) == dict(gc_overlap_available=True, gc_overlap_s=10/1e9, gc_overlap_count=2)
    trace.intervals.extend([(50, 60)] * 2048)
    assert trace.overlap(10, 70) == {'gc_overlap_available': False}


def test_finish_separates_cuda_intervals_and_cpu_rle():
    import time
    from mec_server.workers import TorchAdapter, Submission

    class Event:
        def __init__(self, ms):
            self.ms = ms
            self.syncs = 0
        def elapsed_time(self, other):
            return other.ms - self.ms
        def synchronize(self):
            self.syncs += 1
    class Output:
        def numpy(self):
            return np.array([[1, 1], [2, 2]])
    adapter = object.__new__(TorchAdapter)
    ready = Event(10)
    submission = Submission(None, Output(), ready, Event(0), Event(2), Event(5),
                            dict(cuda_preprocess_submit_start_ns=1_000_000_000,
                                 gpu_completion_observed_ns=1_040_000_000),
                            (2, 2, 1, 0, 0), time.monotonic_ns(),
                            output_ready=Event(7), readback_start=Event(8))
    payload, times = adapter.finish({'configuration': {'application': 'segmentation', 'class_map_id': 'test'}}, submission)
    assert payload['runs'] == [[1, 2], [2, 2]]
    assert times['cuda_pipeline_s'] == .010
    assert times['cuda_submission_to_completion_observed_s'] == .040
    assert abs(times['stream_waiting_est_s'] - .030) < 1e-12
    assert times['gpu_completion_observed_ns'] == 1_040_000_000
    assert times['preprocess_s'] == .002
    assert times['inference_s'] == .003
    assert times['gpu_output_transform_s'] == .002
    assert times['gpu_output_to_readback_s'] == .001
    assert times['gpu_readback_s'] == .002
    assert times['cpu_format_s'] >= times['rle_s']
    assert ready.syncs == 1  # Existing finish readiness check only.


def test_dequeue_to_submission_and_marker_bounds(monkeypatch):
    from mec_server import frame_timing
    from types import SimpleNamespace
    ticks = iter([100, 120, 130, 150, 160, 180])
    monkeypatch.setattr(frame_timing.time, 'monotonic_ns', lambda: next(ticks))
    queries = iter([False, True])
    recorded = []
    event = SimpleNamespace(record=recorded.append, query=lambda: next(queries))
    times = {}
    frame_timing.record_preprocess_submission(event, 'stream', times, 60)
    assert recorded == ['stream']
    assert times['worker_dequeue_to_cuda_submit_s'] == 40/1e9
    assert times['cuda_preprocess_submit_call_s'] == 20/1e9
    frame_timing.observe_preprocess_start(event, times)
    assert 'preprocess_marker_observed_ns' not in times
    frame_timing.observe_preprocess_start(event, times)
    assert times['preprocess_marker_last_not_ready_ns'] == 130  # query entry, not return
    assert times['cuda_submit_to_preprocess_marker_lower_s'] == 10/1e9
    assert times['cuda_submit_to_preprocess_marker_upper_s'] == 80/1e9
    assert times['preprocess_marker_observation_span_s'] == 50/1e9
    frame_timing.observe_preprocess_start(event, times)  # No more queries after success.


def test_marker_ready_on_first_observation_has_zero_lower_bound(monkeypatch):
    from mec_server import frame_timing
    from types import SimpleNamespace
    ticks = iter([200, 220])
    monkeypatch.setattr(frame_timing.time, 'monotonic_ns', lambda: next(ticks))
    times = dict(cuda_preprocess_submit_start_ns=100, cuda_preprocess_submit_end_ns=120)
    frame_timing.observe_preprocess_start(SimpleNamespace(query=lambda: True), times)
    assert times['cuda_submit_to_preprocess_marker_lower_s'] == 0
    assert times['cuda_submit_to_preprocess_marker_upper_s'] == 120/1e9
    assert 'worker_dequeue_to_cuda_submit_s' not in times



def test_stream_wait_estimate_preserves_signed_values_and_unavailable():
    from mec_server.frame_timing import add_stream_wait_estimate
    for missing in ({}, {'cuda_preprocess_submit_start_ns': 100}):
        add_stream_wait_estimate(missing, .01)
        assert 'stream_waiting_est_s' not in missing
        assert 'cuda_submission_to_completion_observed_s' not in missing
    times = dict(cuda_preprocess_submit_start_ns=1_000_000_000,
                 gpu_completion_observed_ns=1_010_000_000)
    add_stream_wait_estimate(times, .010001)
    assert times['stream_waiting_est_s'] < 0  # Never clamp timing error to zero.
