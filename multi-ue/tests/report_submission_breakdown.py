"""Generate a Markdown latency breakdown from the preregistered production trial.

No measurements are rerun. Missing GST timings are explicitly reported as missing.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from report_handoff_latency import percentile


def statistics(values):
    assert values
    return [percentile(values, p) for p in (.5, .95, .99)]


def generate(source, output):
    jobs_path = source / 'production-jobs.jsonl'
    jobs = [json.loads(line) for line in jobs_path.read_text().splitlines()]
    summary = json.loads((source / 'production-summary.json').read_text())
    assert len(jobs) == summary['submitted'] == 48
    assert summary['registration_complete_before_frames']
    assert all(j['stage_times']['cuda_graph'] and j['stage_times']['shared_rgb_direct'] for j in jobs)
    assert all(j['stage_times']['ipc_sender_trace_available'] for j in jobs)
    source_link = Path(os.path.relpath(source, output.parent)).as_posix()

    def durations(start, end, rows=jobs):
        return [(j['stage_times'][end]-j['stage_times'][start])/1e6 for j in rows]

    def table(rows):
        lines = ['| Interval | p50 ms | p95 ms | p99 ms |', '|---|---:|---:|---:|']
        for label, values in rows:
            lines.append('| ' + label + ' | ' + ' | '.join(f'{v:.3f}' for v in statistics(values)) + ' |')
        return '\n'.join(lines)

    def intervals(specs, rows=jobs):
        return table([(label,durations(start,end,rows)) for label,start,end in specs])

    parts = [f'''# Latency breakdown: CUDA graphs with registration completed during setup

Source: **`preregister-production`**, 2026-11-23, DGX Spark / NVIDIA GB10.
Four virtual UEs: YOLO26x detection (12 frames), YOLO26x tracking (two videos,
24 frames), SegFormer-b3 segmentation (12 frames), each at 10 fps. All **48 frames,
including every first frame**, are included unless explicitly stated otherwise.
This is the untraced production-model run; Nsight timings are not mixed into these percentiles.

## What “GST to submission” can and cannot mean in this test

This isolated trial injected already-decoded frames into the receiver handoff; it
did not run a live GStreamer decoder. Its `decoded_monotonic_ns` was taken **after**
copying RGB into the MEC shared-memory slot, immediately before publishing the
decoded notification. The real `Receiver._sample()` places this timestamp after
its Gst buffer map and shared-slot copy too.

Therefore the measured path is:

`RGB already in shared slot → receiver FIFO → engine → worker input queue → worker dequeue → preprocessing enqueue → CUDA graph submission`

**GStreamer decoding, appsink delivery, buffer mapping, and the Gst-to-slot RGB copy
are not timed here.** There is no defensible numeric GST-decoder-ready-to-submit
total from this run. The trial's packet/reception timestamps are synthetic;
its `decode_s` must not be interpreted as measured GStreamer decode time.

## Receiver-to-worker-to-model breakdown

All rows below are host wall-clock durations. “Model submission point” is the
existing `gpu_submit_ns` timestamp, taken immediately before recording the model
start CUDA event and calling `graph.replay()`. It is **before the replay call
returns**, and is not the time GPU inference starts.
''']
    core = [
        ('Receiver publication → engine handler (FIFO/wakeup wait)', 'decoded_monotonic_ns','decoded_handler_monotonic_ns'),
        ('Engine handler → worker queue acceptance', 'decoded_handler_monotonic_ns','worker_submitted_monotonic_ns'),
        ('Queue acceptance → worker dequeue', 'worker_submitted_monotonic_ns','worker_start_monotonic_ns'),
        ('Worker dequeue → model submission point', 'worker_start_monotonic_ns','gpu_submit_ns'),
        ('**Total: publication → model submission point**', 'decoded_monotonic_ns','gpu_submit_ns'),
    ]
    parts += [intervals(core), '''
The four component intervals partition each individual frame's total. Their p50,
p95 or p99 values must **not** be added: each percentile can come from a different
frame. Queue acceptance means `put_nowait()` returned; it does not wait for serialization
or pipe transfer to complete. That interval also includes time until the application's
CPU thread runs and dequeues the job; it is not pure data-transfer time.

## Distinguishing the submission endpoints
''', intervals([
        ('Publication → job accepted into worker queue', 'decoded_monotonic_ns','worker_submitted_monotonic_ns'),
        ('Publication → job dequeued by worker', 'decoded_monotonic_ns','worker_start_monotonic_ns'),
        ('Publication → model submission point (previously reported metric)', 'decoded_monotonic_ns','gpu_submit_ns'),
        ('Publication → `graph.replay()` returned', 'decoded_monotonic_ns','launch_model_call_end_ns'),
        ('Publication → output operations/readback/final event enqueued', 'decoded_monotonic_ns','gpu_launch_end_ns'),
    ]), '''
None of these endpoints means inference has finished. The last endpoint means the
worker has submitted the remaining output work to the CUDA stream, not waited for
its completion.

## Inside the process handoff and worker submission

These are nested or overlapping diagnostic intervals, not extra time to add to
the preceding total. The multiprocessing queue carries descriptors and metadata;
RGB pixels remain in the shared slot.
''', intervals([
        ('Queue put begins → feeder starts serialization', 'ipc_put_begin_ns','ipc_serialize_begin_ns'),
        ('Serialize job metadata', 'ipc_serialize_begin_ns','ipc_serialize_end_ns'),
        ('Write serialized bytes to pipe, including waiting', 'ipc_write_begin_ns','ipc_write_end_ns'),
        ('Deserialize job metadata in worker', 'ipc_deserialize_begin_ns','ipc_deserialize_end_ns'),
        ('Worker dequeue → `adapter.launch()` entry (mapping/validation)', 'worker_start_monotonic_ns','launch_enter_ns'),
        ('CPU enqueue of preprocessing and its RGB-read event', 'launch_preprocess_begin_ns','launch_preprocess_end_ns'),
        ('CPU `graph.replay()` call', 'launch_model_call_begin_ns','launch_model_call_end_ns'),
        ('CPU enqueue of output transformations', 'launch_output_ops_begin_ns','launch_output_ops_end_ns'),
        ('Allocate per-frame pinned result buffer', 'launch_output_alloc_begin_ns','launch_output_alloc_end_ns'),
        ('CPU enqueue of result readback', 'launch_readback_begin_ns','launch_readback_end_ns'),
        ('Entire `adapter.launch()` call', 'launch_enter_ns','launch_exit_ns'),
    ])]
    get_values = statistics(durations('worker_get_begin_ns','worker_get_end_ns'))
    pipe_cpu = [(j['stage_times']['ipc_write_end_thread_cpu_ns']-
                 j['stage_times']['ipc_write_begin_thread_cpu_ns'])/1e6 for j in jobs]
    parts += [f'''
Raw `incoming.get()` wall time has p50/p95/p99 **{get_values[0]:.3f} / {get_values[1]:.3f} /
{get_values[2]:.3f} ms**. This includes idle waiting before a frame exists—especially
between 10-fps arrivals and during setup. It is **not** that frame's queue latency;
use queue-acceptance-to-dequeue above. The raw blocking pipe-read duration has the
same issue, so it cannot isolate IPC transfer duration.

Pipe writing's p95 CPU execution time is **{percentile(pipe_cpu,.95):.3f} ms**, versus
its p95 wall time shown above. Wall time minus thread CPU time combines blocking
and scheduling; these records do not separate OS scheduling from pipe backpressure.
Nor does overlap with another CPU operation, on its own, prove the cause of a wait.

Registration is absent from per-frame mapping/validation in this run. There is no
pinned RGB staging copy or RGB upload. “Pinned result buffer” above is for outputs.

## Publication-to-model-submission by application
''', table([(f"{app} ({sum(j['application']==app for j in jobs)} frames)",
                     durations('decoded_monotonic_ns','gpu_submit_ns',[j for j in jobs if j['application']==app]))
                    for app in ('detection','tracking','segmentation')]), '''
## GPU execution spans, separate from CPU submission

CUDA events record elapsed stream time around preprocessing and around the model.
These spans are not CPU call durations and can include gaps between operations;
they are not a measurement of time queued ahead of the first event. They must not
be added to host latency percentiles as if they were consecutive percentile samples.
''']
    gpu_rows = []
    for app in ('detection','tracking','segmentation'):
        rows = [j for j in jobs if j['application']==app]
        gpu_rows += [(app+' — preprocessing CUDA-event span', [j['stage_times']['preprocess_s']*1000 for j in rows]),
                     (app+' — model CUDA-event span', [j['stage_times']['inference_s']*1000 for j in rows])]
    parts += [table(gpu_rows)]
    setup = [e['registration_ms'] for e in summary['rgb_registrations']]
    firsts = [j for j in jobs if j['frame_id']==0]
    parts += [f'''
## Setup, first frames, and checks

The four worker registration operations took **{', '.join(f'{v:.2f}' for v in setup)} ms**
during preparation, before any frame was published. The slowest of the four first
frames took **{max(durations('decoded_monotonic_ns','gpu_submit_ns',firsts)):.3f} ms** to
the model-submission point. Setup cost was moved earlier, not eliminated.

All 48 frames completed with one terminal outcome and worker result each. Receiver
queue peaks were three notifications / one decoded frame per receiver, with zero
overflows. Tracking order was preserved; workers and shared pools drained cleanly.
The separate hardware trace confirmed registration before preprocessing and zero
HtoD copies. CPU mock tests are not the source of any timing in this report.

## Sources and reproduction

- [Raw per-frame timestamps]({source_link}/production-jobs.jsonl)
- [Trial summary and setup registration durations]({source_link}/production-summary.json)
- [Separate hardware trace checks]({source_link}/trace-report.json)
- [Trial code](../../run_production_handoff_trial.py), [report generator](../../report_submission_breakdown.py)
- [GStreamer timestamp placement](../../../mec/src/mec_server/media.py),
  [worker timestamp placement](../../../mec/src/mec_server/workers.py)

All host differences use the same MEC monotonic clock. Percentiles use linear
interpolation at `(N - 1) × percentile`; values are rounded to 0.001 ms. With only
48 frames (12 per detection/segmentation application), tail estimates are descriptive
of this short run, not guarantees for sustained load. No new test was run for this report.

Source job-file SHA256: `{hashlib.sha256(jobs_path.read_bytes()).hexdigest()}`.

From `multi-ue`:

```bash
python3 tests/report_submission_breakdown.py \\
  tests/evidence/20261123-cuda-registration \\
  --output tests/evidence/20261123-cuda-graphs/registration-setup-latency-breakdown.md
```
''']
    output.write_text('\n'.join(parts))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate(args.source, args.output)
