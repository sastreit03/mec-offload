# MEC frame flow: queues, accounting, and logging

## Receiver handoff and worker submission update

The implementation below supersedes the historical audit preserved after this section.

| Stage | Current behavior |
|---|---|
| Receiver publication | Each receiver owns one FIFO. Defaults are `handoff_notifications: 1024` and `handoff_decoded_frames: 10` (the decoded entries count toward both). The first pending item schedules one thread-safe wakeup. The loop serves up to 32 items, then schedules its own continuation if necessary. |
| Accounting | The same loop applies descriptor, byte progress, and decoded notifications in order. GStreamer still copies the RGB picture before publishing its reference. Receiver overflow records lost notifications and affected frame identities separately from the full FIFO. Collection overflow invalidates the attempt. Snapshots include receiver depths, peaks, and overflow counts. |
| Engine dispatch | After validation and the existing priority decision, `Engine.decoded()` immediately submits an eligible job to its application. The legacy per-video pending limits in existing YAML are accepted for compatibility. Snapshots and resource reports report zero pending scheduler entries and enumerate submitted jobs in `inflight`. |
| Process boundary | One multiprocessing input queue per application carries the RGB array and job metadata. The queue has no configured job-count cap. Health, configured priority, candidate quarantine, deadlines, and collection policy still apply. Successful IPC acceptance installs the frame in `self.inflight`. |
| GPU launch | A single launcher per application dequeues jobs and issues each on its selected CUDA stream. It stages RGB in pinned host memory, requests an asynchronous upload, preprocesses, runs the model, enqueues GPU output transformations and asynchronous result readback, and records a final event. |
| Completion | An observer checks final events across streams. CPU workers format ready results and publish them with job identity and timing. Pinned buffers remain alive through completion. Tracker reordering and result delivery continue on their existing paths. A dead worker's outstanding jobs are reconciled on the engine loop. |

At saturation, a backlog can accumulate before dequeue, on a CUDA stream, or during result handling. The launcher can issue B before same-stream A finishes. Physical memory and driver capacity still constrain throughput.

### Timing and operational acceptance

All host timestamps below use the MEC monotonic clock. Inspect the per-frame stage times. Use `decoded_handler_monotonic_ns - decoded_monotonic_ns` for receiver handoff latency; `worker_start_monotonic_ns - queue_enter_monotonic_ns` for submission plus IPC latency. `worker_submitted_monotonic_ns` marks the parent's return from queue acceptance. The worker timestamps dequeue before launch. `gpu_submit_ns` precedes model submission and `gpu_launch_end_ns` follows enqueueing result readback and its final event. `gpu_completion_observed_ns` records observation of output readiness. Host observation can lag hardware completion.

For this revision, `preprocess_s` measures stream time from upload through preprocessing, `inference_s` is the model's CUDA-event interval in seconds, and `postprocess_s` includes output GPU operations/readback and CPU formatting. `processing_s` measures elapsed worker residence through formatting and can include waiting. Treat old wall-clock stage measurements and new event-based measurements separately in campaign comparisons.

1. From the repository root, run `python -m pytest -q multi-ue/tests/test_frame_handoff.py` in the project's test environment. The CPU tests cover receiver FIFO order and limits, plus a same-priority burst through a spawned worker before polling completions.
2. On the Spark, run `MEC_TEST_CUDA=1 python -m pytest -q multi-ue/tests/test_frame_handoff.py -k cuda_worker` in the CUDA-enabled MEC test environment. The test builds a tiny TorchScript model, extends A's GPU residence, queries A's CUDA event immediately after B has launched, and checks both result IDs and pixel-dependent scores.
3. Run a short multi-UE trial with production artifacts. Check receiver overflow counts, unique terminal outcomes per frame, worker result IDs, per-video tracking order, clean drain, and p95/p99 decoded-to-launch time. Compare detection boxes and segmentation masks against the previous implementation on identical inputs.

### Reproducing acceptance after integration

CPU acceptance, from the repository checkout on either host:

```bash
cd ~/mec-offload-ad/multi-ue
# Only needed when the project environment has not been bootstrapped:
bash scripts/bootstrap.sh
source scripts/environment.sh
python -m pytest -q tests/test_frame_handoff.py
python -m pytest -q tests/test_configuration.py tests/test_cpu_telemetry.py \
  tests/test_execution_failure.py \
  tests/test_plan_ordering.py tests/test_result_delivery.py \
  tests/test_tracker_preparation.py tests/test_receiver_diagnostics.py
```

The CUDA test is skipped unless explicitly enabled. The focused CPU suite covers
the changed contracts and related lifecycle paths. `python -m pytest -q tests`
runs the full suite; see the integration results below for existing failures.

After transferring the reviewed changes to the **MEC Spark** checkout, rebuild:

```bash
cd ~/mec-offload-ad/multi-ue
# A separate tag keeps the existing baseline image available.
docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:queue-acceptance .
docker run --rm --gpus all --ipc=host \
  -e MEC_TEST_CUDA=1 -v "$PWD/tests:/work/multi-ue/tests:ro" \
  multi-ue-mec:queue-acceptance \
  python -m pytest -q tests/test_frame_handoff.py -k cuda_worker
```

The production compose file uses `multi-ue-mec:baseline`; when deploying the
accepted version, `bash scripts/containers.sh build-mec` builds that tag, and
`MEC_CONFIG=mec.collection.yaml bash scripts/containers.sh start-mec` starts it.
The isolated acceptance run does not require starting radio/core containers.

The reproducible [trial harness](../tests/run_production_handoff_trial.py) runs
four virtual UE streams at the configured FPS, using a real clip and production
TorchScript artifacts. It injects descriptor/progress/decoded notifications at
the receiver handoff boundary and exercises the real engine, multiprocessing
workers, CUDA models, ByteTrack, result persistence, and drain. It does **not**
exercise RF, RTP, GStreamer, admission, or radio control. Descriptor byte counts
are synthetic; network/decode timing from this harness is not a measurement.

```bash
cd ~/mec-offload-ad/multi-ue
mkdir -p runs
docker run --rm --gpus all --ipc=host \
  -v "$PWD/tests:/work/multi-ue/tests:ro" \
  -v "$PWD/weights:/work/multi-ue/weights:ro" \
  -v "$PWD/datasets:/media:ro" \
  -v "$PWD/runs:/work/multi-ue/runs" \
  multi-ue-mec:queue-acceptance \
  python tests/run_production_handoff_trial.py \
    --media /media/profile.mov --frames 12
```

Use a fresh `--run-id` when specifying one. Results go to
`runs/<run-id>/mec/trial-summary.json`, with raw jobs/events and the final snapshot
beside it. To compare identical-input outputs, mount a saved pre-update source
tree at `/baseline:ro` and add
`--reference-source /baseline/multi-ue/mec/src/mec_server/workers.py`.
The comparison checks the first and last frame of each stream, including exact
segmentation RLE and detector boxes/scores; it is not an accuracy benchmark.
`--backend mock` runs the same harness with mock inference/tracking, without
requiring `--gpus all`; mock results do not establish GPU behavior.

The reported latency is `(gpu_submit_ns - decoded_monotonic_ns) / 1e6`, using
host monotonic timestamps. It measures decoded-picture publication through model
submission, not the hardware kernel start. The overlap acceptance test separately
queries a CUDA event to establish that B finished launching before A completed.
That proves asynchronous launch progress, not concurrent execution of kernels
on the same CUDA stream. No additional memory-copy optimization is included.

### Integration validation, 2026-11-23

Integrated the ten files from `~/Downloads/mec-queue-update.zip` after comparing
them against the RAN Spark working tree. The untracked system-description document
and the modified OAI submodule were preserved. Built and tested the separate MEC
Spark image `multi-ue-mec:queue-acceptance`, image ID
`sha256:834eb69185f81c6f2d03049c190eaee9ebe5f6b83572ede039f3bc46415956c1`.
The baseline tag and stopped radio/core services were left in place.

- Focused CPU tests above: **36 passed, 1 CUDA test skipped**.
- CUDA-enabled handoff test file on `spark-9d60`, NVIDIA GB10: **4 passed**.
  The actual CUDA-event overlap assertion passed; this is separate from the
  fake-adapter launcher test and mock spawned-worker burst.
- Full CPU suite: **204 passed, 1 skipped, 8 failed, 57 setup errors**. All 65
  failures/errors reproduced with pre-update MEC source (the four changed/new
  test files were excluded from that baseline run: 186 passed, 8 failed,
  57 errors). There were 63 catalog/settings FPS/GoP mismatches and two
  `Runtime.__new__` fixtures missing `associations`. The full suite is not green.

The user selected isolated production-model validation instead of bringing up
the physical UEs/network. The rebuilt-image trial used four virtual streams,
12 frames each, at 10 FPS per stream: detection YOLO26x, two tracking
YOLO26x/ByteTrack streams, and segmentation SegFormer-B3. All four model variants
per application were resident, with one configured priority (-3), MPS disabled,
and three warmup iterations. This short trial establishes correctness at the
tested load, not saturation capacity or reliable tail-latency estimates.

| Measurement | Production CUDA | Separate mock trial |
|---|---|---|
| Submitted / completed | 48 / 48 | 48 / 48 |
| Worker results and terminal outcomes | Exactly one of each per submitted frame | Exactly one of each per submitted frame |
| Receiver peak, each of four receivers | 3 notifications, 1 decoded picture | 3 notifications, 1 decoded picture |
| Receiver overflows | 0 | 0 |
| Decoded-to-model-submit p95 / p99 | 54.42 / 60.93 ms | Not applicable |
| Tracking order, both videos | 0 through 11, strictly increasing | 0 through 11, strictly increasing |
| Drain | Zero frames/inflight/deliveries/tracker activity; all three workers exit 0 | Same |

All eight selected identical-input detector/mask comparisons (first and last
frame of each stream) exactly matched the pre-update adapter. Tracking output
comparison checks detector fields; tracking order is validated separately.
No receiver/background/tracker errors or discarded GPU completions were observed.

Small portable summaries: [production](../tests/evidence/queue-update-20261123/production.json)
and [mock](../tests/evidence/queue-update-20261123/mock.json). Full local logs,
production jobs/events, final snapshot, and replay database are under
`multi-ue/runs/queue-update-validation/` (ignored run evidence). Remote staging
and evidence are under `/tmp/mec-queue-regression/` on the MEC Spark; the remote
checkout itself was not overwritten.

### Optional queue and launch timing

Set `MEC_TRACE_HANDOFF=1` in the **MEC process/container environment** to enable
per-frame diagnostic timestamps. It is off by default. The input queue hooks in
[`queue_timing.py`](../mec/src/mec_server/queue_timing.py) use CPython 3.12 private
queue internals and reject other interpreter versions when enabled. They retain
the existing background feeder, ForkingPickler serialization, pipe transport,
and RGB payload. No extra GPU synchronization or memory-copy optimization is
introduced. Timing calls, small metadata fields, and sender trace bookkeeping
have overhead, so treat traced runs as diagnostics rather than an uninstrumented
performance baseline.

Each boundary has an absolute host-monotonic `*_ns` timestamp and a
`*_thread_cpu_ns` timestamp. Worker results carry these fields into each frame's
`stage_times` in `jobs.jsonl`.

| Fields (begin/end unless noted) | Where / what is measured |
|---|---|
| `ipc_put_begin_ns` → `ipc_serialize_begin_ns` | Parent queue submission to background feeder starting serialization; includes buffer waiting and scheduling |
| `ipc_serialize_*` | Immediately around `ForkingPickler.dumps(job)` in the parent feeder thread |
| `ipc_write_lock_begin_ns` → `ipc_write_begin_ns` | Waiting to acquire the pipe write lock |
| `ipc_write_*` | Immediately around `send_bytes(serialized_job)`; includes pipe backpressure and scheduling |
| `worker_get_*` | Whole `incoming.get()` call in the worker |
| `ipc_get_enter_ns` → `ipc_read_begin_ns` | Entry into the queue implementation through acquiring the read lock |
| `ipc_read_*` | Immediately around `recv_bytes()`; includes waiting for bytes and scheduling |
| `ipc_deserialize_*` | Immediately around `ForkingPickler.loads(data)` in the worker |
| `launch_enter_ns` → `launch_exit_ns` | CPU-side `adapter.launch()` body |
| `launch_pin_*` | Creating the pinned host input |
| `launch_upload_*` | Calling the nonblocking CPU-to-GPU upload |
| `launch_preprocess_*` | Issuing tensor conversion, scaling, resizing, padding, normalization |
| `launch_model_call_*` | Calling the TorchScript model and returning from that call |
| `launch_output_ops_*` | Issuing output transformations |
| `launch_output_alloc_*` | Allocating the pinned CPU output buffer |
| `launch_readback_*` | Requesting nonblocking GPU-to-CPU output copying |

Sender traces are joined by application/video/frame identity when the engine
polls completions. `ipc_sender_trace_available` explicitly reports whether the
sender record was available at that instant; a very late sender thread can cause
it to be false. Missing measurements must not be treated as zero.
`ipc_serialized_bytes` records the serialized job size.

These intervals **are not additive**: pipe reads and writes overlap, and a
worker may enter `get()` before the next frame even exists. A long `get()` can
therefore be idle waiting rather than a frame's delay. The launch measurements
time host API calls, not the completion of asynchronous GPU work. Existing CUDA
event measurements remain the evidence for GPU execution time.

Wall time minus thread CPU time measures time the thread was not executing. It
combines blocking, sleeping, and waiting for a CPU; it does **not** isolate OS
scheduler delay. Very small negative differences are clock-sampling noise, not
negative waiting time. For that distinction, record Linux scheduler wakeup/switch
events with an OS tracing tool (such as `perf sched`) and correlate thread IDs
and timestamps. This instrumentation does not collect those OS events or claim
to measure pure wire/pipe transfer time. CPU clocks from different threads must
not be subtracted (in particular, submission and serialization use different
threads).

With the updated source built into the image, add `-e MEC_TRACE_HANDOFF=1` to the
isolated trial's `docker run` command above. To verify the hooks:

```bash
MEC_TRACE_HANDOFF=1 python -m pytest -q tests/test_queue_timing.py tests/test_frame_handoff.py
# On the MEC Spark's CUDA-enabled environment:
MEC_TRACE_HANDOFF=1 MEC_TEST_CUDA=1 python -m pytest -q tests/test_queue_timing.py tests/test_frame_handoff.py
python tests/report_handoff_latency.py runs/<run-id>/mec/jobs.jsonl
```

The report's `trace_summary` contains wall time, thread CPU time, and their
difference for each measured operation, grouped by application. Older evidence
without the optional fields is still supported.

Validation: the queue/worker tests passed locally (5 passed, 1 CUDA test skipped),
and with tracing plus CUDA on the MEC Spark (6 passed). A separate traced
48-frame production-model trial completed all frames with unique terminal
outcomes, ordered tracking, no queue overflows, and clean worker drain. All 48
frames contained sender, receiver, and launch measurements. Its
[timing breakdown](../tests/evidence/queue-update-20261123/timed-breakdown.json)
is separate from the earlier uninstrumented run. The updated source was mounted
into the existing acceptance image for these tests; rebuild the image before
using the instrumentation without source mounts.

## Previous implementation (2026-11-21 audit)

This describes the previous code as audited on 2026-11-21, from creation of a decoded RGB picture to submission to a worker process. It also explains the earlier packet accounting that this path depends on. For image copies and the later result-delivery path, see [mec_frame_flow_memory.md](mec_frame_flow_memory.md).

## Through-line

Earlier packet events establish the frame's identity, model configuration, and reception timestamps → GStreamer's callback creates the RGB array and **posts a decoded event to the shared event queue** → the engine **validates the frame and retains the picture** → **records decode timing and selects/logs its priority** → **checks per-video pending-frame and memory limits** → **queues frame identifiers by application and priority** → the engine handles other events and **polls worker completions** → **rechecks the next queued frame and worker availability** → **submits the job to the worker's input queue and records it as in flight** → the multiprocessing queue serializes and transfers the job to the worker process.

The queues organize necessary work, but are not themselves requirements of the design. Replacing them must preserve validation, accounting, memory bounds, and safe ownership of frame state.

## Terms and queue ownership

- **Picture:** a decoded RGB NumPy array containing pixel values.
- **Descriptor:** metadata identifying the video, frame, media session, configuration, and transmission plan. Each RTP carries the descriptor for that frame.
- **Frame record:** the engine's record of one frame, including its picture reference, configuration, timestamps, and processing state.
- **Priority:** selects a processing slot and CUDA stream within an application. Collection mode uses the first configured priority.
- **FIFO:** first in, first out. The oldest queued entry is considered first.
- **Resolved frame:** a frame already assigned a terminal outcome, such as completed or failed.

| Queue or holding area | Ownership and contents |
|---|---|
| GStreamer appsink | Per video; holds decoded samples before the callback pulls them |
| `self.events` | Shared by all UEs/applications; decoded-picture references, packet descriptors/progress, failures, and diagnostics; capacity 1,024 events |
| `self.frames` | Shared frame-record dictionary; retains images and processing state, but is not a FIFO |
| `self.pending[(application, priority)]` | FIFO shared by UEs using that application/priority; holds video/frame identifiers, not image copies |
| Worker input queue | One per application process; transfers jobs containing images and configuration; capacity equals the number of configured priorities |
| Worker completion queue | Shared by application workers; returns results and timings; capacity 16 messages |
| Recorder queue | Separate logging queue; holds copied evidence records for later JSON serialization and disk writing; capacity 16,384 records |

The engine permits only one outstanding job per application/priority. With one priority, all UEs using an application share one processing slot.

## 1. Earlier packet events establish the frame record

These events are posted before the decoded-picture event, which occurs after the frame data is copied into an RGB NumPy array, but may still be waiting for the engine to handle them when the RGB array is created.

**Descriptor handling:**

- Reject unknown videos, mismatched media-session identifiers, terminal videos, and already resolved frames.
- Detect duplicate or conflicting descriptors.
- Check that configuration and transmission-plan versions exist and agree.
- Promote a prepared successor video when appropriate and account for intentional suspension gaps.
- Create the frame record and attach its selected model configuration.
- Record the first-packet timestamp and increment the video's received-frame count.
- Observe configuration changes and record descriptor/configuration evidence.
- Establish radio accounting and queue descriptor information for EdgeRIC, the radio controller.

**Reception-progress handling:**

- Update received-byte counts and packet timestamps.
- Count complete encoded frames for collection validation, without double counting them.
- When reception is complete, set `complete_ns` and the complete-reception timestamps.
- Update network estimates and the frame deadline where applicable.
- Forward final reception progress toward EdgeRIC and clear the completed radio-accounting entry.

Bypassing the decoded-event queue without preserving this ordering could submit a picture before its frame record or `complete_ns` exists.

Source: [engine.py](../mec/src/mec_server/engine.py), `receive_descriptor`, `_observe_plan`, `progress_event`, and `flush_progress`.

## 2. GStreamer posts the decoded picture

Immediately before creating the RGB array, the appsink callback matches the decoded sample to its descriptor and checks the decoded-buffer size limit. Missing metadata or an oversized image prevents normal submission.

After the independent NumPy array exists, the callback:

1. Captures `decoded_ns`.
2. **Posts a decoded event to the shared event queue**, containing the descriptor, picture reference, and timestamp. This does not copy pixels or serialize JSON.
3. Updates receiver-side decode bookkeeping: removes this frame from the undecoded list; an IDR/keyframe also closes older unrecoverable decode gaps by posting failure notifications.

If `self.events` is full, `post()` increments `media_bridge_errors`. For a decoded event, it schedules frame failure on the engine loop: `collection_event_overflow` in collection mode, otherwise `buffer_overflow`.

Posting an internal event is not the same as writing to `events.jsonl`.

Source: [media.py](../mec/src/mec_server/media.py), `_sample` and `_decoded_frame`; [engine.py](../mec/src/mec_server/engine.py), `post`.

## 3. The engine validates the decoded frame

The engine reads the event and **validates the frame and retains the picture**. Before retaining it, `decoded()` applies these checks:

| Condition | Action |
|---|---|
| Collection scenario already invalid | Ignore the event |
| Frame/video record missing, or frame already resolved | Ignore the event |
| Complete encoded reception has not been recorded | Fail with `incomplete_encoded_access_unit` |
| Input closed, outside collection mode | Fail with `input_closed` |
| Selected model candidate quarantined after an execution failure | Fail with `execution_candidate_quarantined` |
| Frame belongs to a suspended transmission plan | Fail with `session_suspended` |

Some checks return without a separate log entry. Failure paths resolve the frame and produce failure evidence/results. Unexpected exceptions during event handling are logged as `receiver_event_error`.

For an accepted frame, the engine sets state `PRIORITY_PENDING`, assigns `f.rgb = rgb`, and retains the decoded timestamp. These assignments keep references; they do not copy the image. An optional dashboard preview also receives the picture reference.

Source: [engine.py](../mec/src/mec_server/engine.py), `decoded`.

## 4. Decode timing and priority decision

The engine **records decode timing and selects/logs its priority**:

- `decoded_monotonic_ns`: when the callback finished preparing the NumPy picture.
- `decode_s`: `(decoded_ns - complete_ns) / 1e9`. This includes the remaining receive-to-decode pipeline and picture copies, not just decoder computation.
- In collection mode, priority is `priority_streams[0]`.
- Outside collection mode, the policy considers the latency target, network estimate, elapsed waiting time, historical queue wait, and expected processing time.
- Priority-decision start/end timestamps and duration are saved in the frame record.
- A `priority_decision` event records the selected priority, policy, frame identity, network estimate, and waiting time.
- If no priority is usable, the frame fails with `nonpositive_slack`.

Frame timing fields are retained for later `jobs.jsonl` evidence. The decision event is queued for `events.jsonl`. Logging has some immediate cost: the recorder deep-copies the evidence and enqueues it; its dedicated thread later serializes and writes it.

Sources: [engine.py](../mec/src/mec_server/engine.py), `decoded`; [recording.py](../mec/src/mec_server/recording.py), `Recorder.put` and `_writer`.

## 5. Pending-frame and memory accounting

The engine **checks per-video pending-frame and memory limits** by scanning frame records for the same video in states `PRIORITY_PENDING` or `QUEUED`. It counts those frames and sums their RGB-array `nbytes`.

- The newly decoded picture is included.
- Pictures still waiting in `self.events` are not included in this pending-image sum.
- Frames already marked `PROCESSING` are not included.
- This is therefore not a measurement of total MEC memory use.

If a limit is exceeded:

- **Collection:** invalidate the scenario, log `collection_run_invalid` with pending counts/bytes, and fail the frame with `collection_buffer_limit`.
- **Other modes:** resolve the pending frame with the lowest frame ID as `buffer_overflow`, repeating until within limits.

The scan and byte summation occur for each accepted decoded frame. They consume CPU time independently of FIFO insertion.

## 6. Application FIFO insertion

After another resolved-frame check, the engine **queues frame identifiers by application and priority**:

1. Capture `queue_ns` and save `queue_enter_monotonic_ns`.
2. Set state `QUEUED`.
3. Append `(video_id, frame_id)` to the corresponding FIFO.

The picture remains in the frame record. There is no separate normal-path enqueue event written to `events.jsonl`; its timestamp appears later in frame evidence.

UEs sharing an application/priority share this FIFO. There is no per-UE round-robin selection. A burst of decoded pictures from one UE can precede another UE's picture.

## 7. Shared engine work before dispatch

Each loop iteration checks radio-report/control health, then handles up to 1,024 shared events, stopping earlier if the queue becomes empty. It does not interleave worker polling with individual events in that batch.

Before dispatch, it also:

- Checks collection media progress.
- Flushes pending reception-progress updates.
- Checks receiving-frame deadlines outside collection mode.
- Detects exited worker processes and recording failures; these can invalidate collection.
- **Polls worker completions**, releases their busy slots, updates processing/queue-wait histories, and handles their results or tracking state.

Worker processes run independently during this work. A finished worker's slot remains busy in the engine until its completion is polled. Other UEs' events and other applications' completions can therefore delay the next dispatch.

The loop sleeps for 2 ms after dispatch and remaining lifecycle work. This is not a guaranteed 2 ms dispatch interval: all work in the iteration takes additional time.

Source: [engine.py](../mec/src/mec_server/engine.py), `tick`.

## 8. Dispatch checks and worker submission

For each application/priority FIFO, the engine **rechecks the next queued frame and worker availability**:

1. Require the application worker to be ready and its priority slot not busy.
2. Remove the oldest queued identifiers.
3. Skip entries whose frame no longer exists or whose state is no longer `QUEUED`.
4. Fail candidates that became quarantined while waiting.
5. Outside collection mode, fail frames whose deadlines expired. Collection mode does not reject processing work merely for being late.
6. Build a job dictionary containing frame identifiers, priority, model configuration, and the RGB-array reference. This does not copy pixels.

`Workers.submit()` rechecks availability and tries a nonblocking insertion into the application's multiprocessing input queue. If unavailable or full, the engine returns the frame identifiers to the front of the pending FIFO. If accepted, it marks the application/priority slot busy.

The engine then **submits the job to the worker's input queue and records it as in flight** by retaining candidate ID, queue-entry time, submission time, application, and priority in `self.inflight`, and setting frame state `PROCESSING`.

Two measurement limitations matter:

- `submitted_ns` currently uses the timestamp captured near the beginning of the loop iteration, not a fresh timestamp at submission.
- Successful `put_nowait()` means the multiprocessing queue accepted the job. Its feeder thread serializes and transfers the job afterward; acceptance is not proof that transfer has completed or worker execution has started.

There is no additional normal-path per-frame submission log here. The retained state supports completion handling and later timing evidence. `queue_wait_s` is calculated later as worker-start time minus queue-entry time, so it includes FIFO waiting, dispatch, process transfer, and worker scheduling.

Sources: [engine.py](../mec/src/mec_server/engine.py), `tick`; [workers.py](../mec/src/mec_server/workers.py), `available`, `submit`, and `poll`.

## Responsibilities to preserve if these queues change

- Establish validated frame/configuration state before using the decoded picture.
- Preserve packet counts, reception timestamps, and EdgeRIC accounting.
- Reject resolved, suspended, invalid, or quarantined work.
- Select the priority and retain decision/timing evidence.
- Bound retained pictures and explicitly handle overload.
- Reserve worker capacity and track accepted jobs until completion.
- Keep state changes safe: the GStreamer callback and engine run on different threads. Calling engine state-mutating methods directly from the callback would require a new synchronization design.

If a worker is occupied, a new picture must still wait somewhere or receive an explicit failure/drop outcome. Removing a deque alone does not remove that capacity constraint.
