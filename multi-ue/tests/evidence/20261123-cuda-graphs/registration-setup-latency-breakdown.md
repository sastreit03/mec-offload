# Latency breakdown: CUDA graphs with registration completed during setup

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

| Interval | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| Receiver publication → engine handler (FIFO/wakeup wait) | 0.521 | 1.211 | 1.491 |
| Engine handler → worker queue acceptance | 0.053 | 0.161 | 0.523 |
| Queue acceptance → worker dequeue | 0.866 | 2.000 | 3.263 |
| Worker dequeue → model submission point | 0.243 | 0.424 | 1.973 |
| **Total: publication → model submission point** | 1.752 | 3.874 | 4.227 |

The four component intervals partition each individual frame's total. Their p50,
p95 or p99 values must **not** be added: each percentile can come from a different
frame. Queue acceptance means `put_nowait()` returned; it does not wait for serialization
or pipe transfer to complete. That interval also includes time until the application's
CPU thread runs and dequeues the job; it is not pure data-transfer time.

## Distinguishing the submission endpoints

| Interval | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| Publication → job accepted into worker queue | 0.602 | 1.311 | 1.594 |
| Publication → job dequeued by worker | 1.499 | 3.131 | 3.854 |
| Publication → model submission point (previously reported metric) | 1.752 | 3.874 | 4.227 |
| Publication → `graph.replay()` returned | 1.827 | 3.921 | 4.689 |
| Publication → output operations/readback/final event enqueued | 1.982 | 4.334 | 5.238 |

None of these endpoints means inference has finished. The last endpoint means the
worker has submitted the remaining output work to the CUDA stream, not waited for
its completion.

## Inside the process handoff and worker submission

These are nested or overlapping diagnostic intervals, not extra time to add to
the preceding total. The multiprocessing queue carries descriptors and metadata;
RGB pixels remain in the shared slot.

| Interval | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| Queue put begins → feeder starts serialization | 0.535 | 1.835 | 2.858 |
| Serialize job metadata | 0.035 | 0.077 | 0.110 |
| Write serialized bytes to pipe, including waiting | 0.052 | 0.613 | 0.774 |
| Deserialize job metadata in worker | 0.015 | 0.048 | 0.062 |
| Worker dequeue → `adapter.launch()` entry (mapping/validation) | 0.101 | 0.195 | 0.226 |
| CPU enqueue of preprocessing and its RGB-read event | 0.034 | 0.129 | 1.580 |
| CPU `graph.replay()` call | 0.047 | 0.543 | 0.839 |
| CPU enqueue of output transformations | 0.024 | 0.331 | 1.465 |
| Allocate per-frame pinned result buffer | 0.028 | 0.058 | 0.073 |
| CPU enqueue of result readback | 0.029 | 0.047 | 0.059 |
| Entire `adapter.launch()` call | 0.310 | 1.106 | 4.024 |

Raw `incoming.get()` wall time has p50/p95/p99 **98.709 / 124.287 /
528.703 ms**. This includes idle waiting before a frame exists—especially
between 10-fps arrivals and during setup. It is **not** that frame's queue latency;
use queue-acceptance-to-dequeue above. The raw blocking pipe-read duration has the
same issue, so it cannot isolate IPC transfer duration.

Pipe writing's p95 CPU execution time is **0.083 ms**, versus
its p95 wall time shown above. Wall time minus thread CPU time combines blocking
and scheduling; these records do not separate OS scheduling from pipe backpressure.
Nor does overlap with another CPU operation, on its own, prove the cause of a wait.

Registration is absent from per-frame mapping/validation in this run. There is no
pinned RGB staging copy or RGB upload. “Pinned result buffer” above is for outputs.

## Publication-to-model-submission by application

| Interval | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| detection (12 frames) | 1.761 | 4.225 | 4.234 |
| tracking (24 frames) | 1.735 | 3.132 | 3.486 |
| segmentation (12 frames) | 1.830 | 2.804 | 2.981 |

## GPU execution spans, separate from CPU submission

CUDA events record elapsed stream time around preprocessing and around the model.
These spans are not CPU call durations and can include gaps between operations;
they are not a measurement of time queued ahead of the first event. They must not
be added to host latency percentiles as if they were consecutive percentile samples.

| Interval | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| detection — preprocessing CUDA-event span | 0.144 | 3.274 | 4.085 |
| detection — model CUDA-event span | 28.729 | 33.220 | 33.244 |
| tracking — preprocessing CUDA-event span | 2.498 | 4.573 | 4.655 |
| tracking — model CUDA-event span | 23.498 | 28.932 | 32.799 |
| segmentation — preprocessing CUDA-event span | 2.378 | 4.641 | 4.676 |
| segmentation — model CUDA-event span | 59.827 | 61.395 | 62.289 |

## Setup, first frames, and checks

The four worker registration operations took **57.63, 55.94, 62.33, 62.45 ms**
during preparation, before any frame was published. The slowest of the four first
frames took **4.237 ms** to
the model-submission point. Setup cost was moved earlier, not eliminated.

All 48 frames completed with one terminal outcome and worker result each. Receiver
queue peaks were three notifications / one decoded frame per receiver, with zero
overflows. Tracking order was preserved; workers and shared pools drained cleanly.
The separate hardware trace confirmed registration before preprocessing and zero
HtoD copies. CPU mock tests are not the source of any timing in this report.

## Sources and reproduction

- [Raw per-frame timestamps](../20261123-cuda-registration/production-jobs.jsonl)
- [Trial summary and setup registration durations](../20261123-cuda-registration/production-summary.json)
- [Separate hardware trace checks](../20261123-cuda-registration/trace-report.json)
- [Trial code](../../run_production_handoff_trial.py), [report generator](../../report_submission_breakdown.py)
- [GStreamer timestamp placement](../../../mec/src/mec_server/media.py),
  [worker timestamp placement](../../../mec/src/mec_server/workers.py)

All host differences use the same MEC monotonic clock. Percentiles use linear
interpolation at `(N - 1) × percentile`; values are rounded to 0.001 ms. With only
48 frames (12 per detection/segmentation application), tail estimates are descriptive
of this short run, not guarantees for sustained load. No new test was run for this report.

Source job-file SHA256: `0b5b70cac783ca235360b2c849e3feca2f17acdd42f06085624fa85f267d4294`.

From `multi-ue`:

```bash
python3 tests/report_submission_breakdown.py \
  tests/evidence/20261123-cuda-registration \
  --output tests/evidence/20261123-cuda-graphs/registration-setup-latency-breakdown.md
```
