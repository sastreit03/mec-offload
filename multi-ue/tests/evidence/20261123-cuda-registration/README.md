# CUDA registration before receiver readiness — 2026-11-23

Implemented on the RAN checkout and validated in isolated containers on the DGX
Spark (192.168.71.130, NVIDIA GB10). Image:
`multi-ue-mec:cuda-registration-acceptance`, ID
`sha256:2989b98055fce6fa83505394cdfba0d27284cb0e0e481377bce60f7e6ebdcdfc`.
No running service or radio/core deployment was changed.

## Behavior

After the receiver reserves its shared-memory pool, the engine sends region name
and size to that application's worker. It asynchronously waits for a registration
acknowledgement before returning initial admission or successor readiness. The
control loop continues servicing requests. No frame is fabricated or copied for
registration. `rgb_registered` events record the worker's preparation duration.

The registration remains protected from cache eviction throughout that receiver's
lifetime. Finalization or setup rejection retires it; unregistration waits for any
outstanding GPU readers. Timeout (30 seconds), cancellation and worker exit fail
preparation and clean up pending ownership. Cache capacity matches the bounded
receiver-port capacity (twice `max_videos`, including prepared successors).
Direct/offline adapter callers may still use lazy registration.

The cost moves into preparation; it is not removed. A registration command runs on
the application's CPU submission thread. Preparing a receiver during existing
traffic can therefore delay that application's other submissions. The trial below
prepares all four receivers before publication, matching collection readiness
before the common release gate.

## Production results, including the first frames

Same four virtual UEs and production models as the preceding experiment:
YOLO26x detection, two YOLO26x tracking videos, SegFormer-b3 segmentation;
12 frames each at 10 fps. Frames enter the decoded receiver handoff; this is not
an RF or live decoder trial.

| Measurement | Result |
|---|---:|
| Frames submitted/completed | 48 / 48 |
| Decoded-to-model-submit p95, all 48 frames | 3.87 ms |
| Decoded-to-model-submit p99, all 48 frames | 4.23 ms |
| Slowest first frame across four receivers | 4.24 ms |
| Registration duration during setup | 55.94–62.45 ms per region |
| Receiver queue peaks | 3 notifications / 1 decoded frame each |
| Queue overflows / pool exhaustions | 0 / 0 |
| Shared-pool peak | 1 slot per receiver |
| Largest serialized metadata job | 1,109 bytes |

Every submitted frame had exactly one terminal outcome and one worker result.
Both trackers processed 0–11 in order. Workers exited zero; shared pools drained
and unlinked with no remaining input leases. All four registration acknowledgements
preceded the first decoded publication timestamp. These are short-run host
submission timings, not GPU execution-start timings or a controlled throughput
benchmark. The preceding lazy-registration run's all-frame p95/p99 was
58.43/103.31 ms; no first frames are excluded from the new numbers.

`production-summary.json`, `production-jobs.jsonl` and `latency-report.json` retain
the untraced measurements. This change does not alter preprocessing/model math;
the preceding CUDA-graph acceptance contains exact production-output comparisons.

## Hardware trace and tests

`trace-report.json`: all checks passed. Each worker completed its CUDA host
registrations before its first preprocessing kernel. All 48 frames used direct
RGB preprocessing and graph replay, with zero HtoD copies; only result readbacks
remain. Five tracking-worker CPU replay calls overlapped earlier GPU graph kernels
in this traced run. This is hardware evidence, separate from the CPU mock/control
checks. Trace instrumentation affects timing; headline latency comes from the
untraced run.

`tests-final.log`: **83 passed**, one dependency deprecation warning. Includes
empty-region preparation, protected registrations, deferred unregistration,
admission/control responsiveness, initial/successor readiness, registration failure,
cancellation, timeout, worker exit, metadata control messages, CUDA graph/storage
reuse, receiver queues, tracking preparation, configuration and lifecycle tests.

The expanded collection-start/stop selection had **52 passes and 23 fixture setup
errors** (`tests-complete.log`). Those fixtures load the existing development YAML
whose FPS/GoP differs from the current catalog. They fail before exercising the
changed behavior; their configuration was preserved. Earlier logs also record
missing test-only imports, resolved by mounting UE and EdgeRIC source directories.
The focused 83-test suite and production/trace runs above completed successfully.

## Reproduction

From `multi-ue` on the DGX:

```bash
docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:cuda-registration-acceptance .
MEC_GRAPH_IMAGE=multi-ue-mec:cuda-registration-acceptance \
  bash tests/run_cuda_graph_acceptance.sh "$PWD/runs/registration-acceptance-new"
```

The runner includes `test_rgb_preparation.py` and uses the updated production trial,
which now requires preparation acknowledgements before frame publication. CPU-only
preparation tests can run in the MEC Python/GI environment with:

```bash
python -m pytest -q tests/test_rgb_preparation.py tests/test_queue_timing.py
```

Full trace artifacts remain on DGX under `/tmp/mec-cuda-registration/runs/`:
`registration_trace.nsys-rep` and `registration_trace.sqlite`. Hashes are in
`trace-sha256.txt`. Full runs are `preregister-production` and `preregister-traced`.
`source-sha256.txt` identifies the implementation and relevant test/report sources.
