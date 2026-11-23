# CUDA graphs with direct shared RGB preprocessing — 2026-11-23

Implemented and tested on the DGX/MEC Spark (`spark-9d60`, 192.168.71.130), NVIDIA
GB10. Acceptance image: `multi-ue-mec:cuda-graphs-acceptance`, immutable ID
`sha256:3ea508443242d6379192d711cf832439835eec7532d053283110706064983e7c`.
PyTorch 2.10.0a0+b4e4ee81d3.nv25.12, CUDA 13.1, host driver 580.173.02.
The running service and radio/core containers were not deployed or started.

## Implementation

GPU preprocessing reads the CUDA-registered POSIX RGB region and writes directly
into the model/priority graph's fixed FP16 NCHW input. The worker sends a slot
release only after the preprocessing CUDA event completes. Inference and output
handling continue on the same stream. Graph inputs, outputs and private pools are
separate for each model/priority; each frame's output is copied to its own result
buffer before another replay can overwrite graph output. Worker drain precedes
unregistration. Graphs are prepared before readiness; startup fails on capture
errors instead of silently falling back. `cuda_graphs: false` restores the old path.

YOLO's exported CPU batch-index `arange` caused capture to fail. Its device is
rewritten to CUDA in memory before execution; weight/artifact files are unchanged.
The initial kernel's explicit FMA suppression also caused small FP16 discrepancies;
normal CUDA FMA compilation and NCHW storage reproduce the existing preprocessing
bit-for-bit in all four tested resize/normalization cases. Earlier failure logs are
retained; `tests-final.log`, `production-fixed.log` and `trace-report.json` are final.

## Results

- `tests-final.log`: 32 passed, including real GPU tests and CPU memory/queue tests.
  GPU coverage: exact preprocessing, four independent graph inputs (two models ×
  two priorities), retained outputs, slot reuse before deliberately delayed GPU
  inference finishes, and exported CPU-index relocation.
- `cpu-regression.log`: 34 passed, one dependency deprecation warning. Covers
  configuration, telemetry, execution failure, plan order, result delivery,
  tracker preparation, receiver diagnostics and observer lifetime. One lifetime
  test overlaps the preceding 32-test selection; these are 65 distinct tests.
- Untraced production trial: four virtual UEs, 12 frames each, 10 fps each;
  YOLO26x detection, two YOLO26x tracking streams, SegFormer-b3 segmentation.
  All 48 frames used direct RGB preprocessing and graph replay. Exactly one
  terminal outcome and worker result per submitted frame, all completed.
- Receiver peaks: three notifications / one decoded frame per receiver;
  zero overflows. Shared-pool peaks: 1, 2, 2, 1 slots; no exhaustion.
  All pools drained/unlinked, no outstanding input leases, all workers exited 0.
- Both tracking sequences were 0–11. Eight first/last-frame output comparisons
  against `baseline_workers.py` matched exactly. Model hashes are in
  `production-summary.json`. The largest serialized IPC job was 1,109 bytes.
- All catalog model variants captured successfully at priority -3: four YOLO
  graphs in detection, four in tracking, four SegFormer graphs in segmentation.
  Multi-priority storage/reuse was tested with tiny models, not a full production
  load at all three deployment priorities.

### Latency and initial registration

Host decoded-to-model-submit (not hardware execution start):

| Frames included | Count | p95 ms | p99 ms |
|---|---:|---:|---:|
| All frames | 48 | 58.43 | 103.31 |
| After each receiver's first frame | 44 | 4.22 | 33.78 |
| After each receiver's first two frames | 40 | 3.53 | 4.10 |

These are explicitly different subsets of one short run, not interchangeable
performance claims. First frames register each approximately 256 MiB region with
CUDA. The separate traced run measured four `cudaHostRegister` calls of 40.46,
61.62, 64.90 and 79.72 ms. Later jobs can queue behind these registrations. Moving
registration into receiver preparation is a possible follow-up; this implementation
registers lazily. The all-frame graph-replay CPU-call p95/p99 was 0.336/0.442 ms.
See `latency-report.json` and `production-jobs.jsonl`; this is not a controlled
before/after throughput benchmark.

### Real hardware trace, separate from mocks

`trace-report.json` uses each worker's NVTX range after capture/warmup:
48 direct RGB preprocessing kernels, 48 graph replays, **zero HtoD copies**.
Only 48 DtoH result copies remain. Metadata-only IPC and the pool's single-copy
counters are checked separately from the CUDA trace; the trace does not measure
physical cache/DRAM transactions.

In the tracking worker, 12 CPU graph-submission calls overlapped GPU kernels from
an earlier graph of that same worker. Ten calls began before an earlier graph's
GPU execution had finished. This demonstrates asynchronous CPU submission during
GPU inference; it does not imply simultaneous execution on a single CUDA stream.
The GPU slot-reuse test independently observes the RGB-read event complete while
inference's final event is still incomplete. Its artificial delay makes that a
synthetic hardware test, distinct from the production trace.

`mock-summary.json` is a separate CPU mock run: 48/48 completed, ordered tracking,
clean drain, no overflow. It supplies no evidence of CUDA performance or overlap.

The production trial injects already decoded video frames into the receiver
handoff. It does not exercise radios or a live GStreamer decoder. Actual Gst buffer
maps and duplicate/stride behavior are covered by the shared-memory tests.

## Reproduction and retained artifacts

Build from `multi-ue`:

```bash
docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:cuda-graphs-acceptance .
MEC_GRAPH_REFERENCE_SOURCE="$PWD/tests/evidence/20261123-cuda-graphs/baseline_workers.py" \
  bash tests/run_cuda_graph_acceptance.sh "$PWD/runs/graphs-acceptance-new"
```

The runner executes the GPU tests, untraced production trial, separate traced trial,
SQLite export and reports. The baseline source argument is optional. Use a fresh
output directory. CPU acceptance needs the MEC Python/GI environment but no GPU:

```bash
python -m pytest -q tests/test_graph_lifetime.py tests/test_shared_rgb.py \
  tests/test_frame_handoff.py tests/test_queue_timing.py
```

Full trace files remain on the DGX at
`/tmp/mec-cuda-graphs/runs/graphs_trace.nsys-rep` (112 MiB) and
`/tmp/mec-cuda-graphs/runs/graphs_trace.sqlite` (298 MiB). Their SHA256 values are
in `trace-sha256.txt`; these large files are not copied into the repository.
Full trial directories are alongside them (`graphs-production-fixed`,
`graphs-traced`, `graphs-mock`). Local JSON summaries, raw untraced job timings,
logs and the baseline source are preserved here. `source-sha256.txt` identifies
implementation and test sources used for review/reproduction.
