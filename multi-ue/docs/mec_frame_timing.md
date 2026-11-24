# MEC Frame Timing Description

## Current timing audit (2026-11-23)

This section describes the running code's frame path. The historical description
below is retained for reference.

GStreamer decodes the video and converts each picture to RGB pixels. The receiver
retrieves the picture from appsink, copies the pixels into shared memory, and
notifies the engine. The engine checks the frame and sends its descriptor to the
application's worker process. The worker uses the descriptor to access the pixels
and submits preprocessing, inference, and output handling to a CUDA stream.

A CUDA stream is an ordered sequence of GPU operations. A CUDA event is a marker
in that sequence: it becomes complete when the GPU reaches it. The **observer**
is a CPU thread that checks whether these markers have completed. Once the final
marker is complete, it asks a **completion thread** to turn the output into Python
result objects. The engine receives those objects, converts the result to JSON
once, and passes the same JSON text to the WebSocket sender and the replay database
writer. Sending and database writing proceed independently.

Host `*_ns` timestamps use `time.monotonic_ns()` on the **same MEC host** and are
expressed in nanoseconds. `*_s` durations are seconds. CUDA durations measure time
between events in the frame's CUDA stream. Do not subtract a CUDA timestamp from
a host timestamp, or a UE monotonic timestamp from a MEC monotonic timestamp.
Thread CPU time measures time spent executing on that thread; wall time also
includes periods when the thread is blocked or not scheduled to run. Missing
measurements are unavailable, not zero. Some intervals contain or overlap others;
they must not all be added together.

| Timestamp interval or metric | What the code is doing during this time |
|---|---|
| `first_packet_monotonic_ns` → `complete_reception_monotonic_ns` | Receiving the encoded frame's packets. Starts when the first packet is observed at MEC and ends when complete encoded reception is recorded. This is the packet arrival span at MEC, not the frame's entire network journey. |
| `decode_s` | Turning a fully received encoded frame into RGB pixels ready for the engine. Starts at complete reception and ends after the receiver has copied the decoded picture into shared memory. Includes GStreamer waiting, decoding, RGB conversion and the receiver's copy. |
| `complete_reception_monotonic_ns` → `appsink_enter_ns` | Waiting for GStreamer to produce the decoded RGB sample and call our Python receiver. Ends immediately before the callback asks appsink for that sample. Does not isolate decoder computation. |
| `appsink_enter_ns` → `appsink_pulled_ns` | Retrieving the available decoded sample from appsink's internal queue with `sink.emit("pull-sample")`. Python receives a reference to the sample; this call does not copy the whole picture. Includes call overhead and any blocking or descheduling during the call, but not the sample's earlier wait in appsink. |
| `image_extract_start_ns` → `decoded_monotonic_ns` | Accessing the sample's RGB pixels and making them available in shared memory. Starts before mapping the GStreamer buffer and ends after creating the NumPy view, copying the visible pixels, releasing the buffer mapping and preparing the handoff. |
| `shared_rgb_copy_start_ns` → `shared_rgb_copy_end_ns` | Copying the RGB picture into a shared-memory slot for the worker. Starts before calling the pool's copy method and ends when it returns the slot reference. Includes obtaining the pool lock, finding a free slot, allocating storage if needed, copying pixels and creating the reference. This is part of the image-extraction interval above. |
| `decoded_monotonic_ns` → `decoded_handler_monotonic_ns` | Waiting for the engine to handle the receiver's decoded-frame notification. The pixels are already copied; the notification waits in the receiver's FIFO and for the engine event loop to run it. |
| `decoded_handler_monotonic_ns` → `decoded_validation_end_ns` | Checking whether the engine can accept the decoded frame. Includes finding its frame/video records, checking reception and lifecycle state, and attaching receiver timing information. |
| `decoded_handler_monotonic_ns` → `worker_submitted_monotonic_ns` | Handling the decoded-frame notification and handing the job to the worker input queue. Includes validation, optional preview handling, priority selection, evidence logging and submission. The ending timestamp means the parent's submission call returned, not that the worker received the job. |
| `worker_process_transfer_s` | Passing the job from the engine to the worker process. Starts at `queue_enter_monotonic_ns`, just before building/submitting the job, and ends at `worker_start_monotonic_ns`, immediately after the worker retrieves it. Includes preparation, serialization, process communication, input-queue waiting and worker scheduling. Does not include waiting in the CUDA stream. |
| `worker_input_mapping_s` | Letting the worker access the shared RGB pixels after retrieving the job. Includes opening or acquiring the shared-memory mapping and obtaining its NumPy image view. |
| `worker_dequeue_to_cuda_submit_s` | Preparing the retrieved job for CUDA submission. Starts when the worker retrieves the descriptor and ends immediately before it calls `record(stream)` on the preprocessing-start event. Includes shared-memory mapping and image geometry/setup; the non-graph path also allocates pinned input storage and copies the RGB pixels into it. |
| `cuda_preprocess_submit_call_s` | Asking CUDA to place the preprocessing-start marker in the selected stream. Measures the CPU duration of `record(stream)`, from `cuda_preprocess_submit_start_ns` to `cuda_preprocess_submit_end_ns`. It does not wait for the GPU to reach the marker. |
| `cuda_submit_to_preprocess_marker_lower_s` | The minimum delay we can establish between submitting the start marker and the GPU reaching it. Uses the last CPU check that found the marker incomplete. If the first check finds it complete, the lower bound is zero; that does not prove there was no wait. |
| `cuda_submit_to_preprocess_marker_upper_s` | The maximum delay allowed by our observations between submitting the start marker and the GPU reaching it. Ends at the CPU's first observation that the marker is complete, so late polling can make this bound much larger than the actual delay. |
| `preprocess_marker_observation_span_s` | How wide the CPU's observation window is for completion of the start marker. Measures from the last unsuccessful check's entry, or submission start if there was none, to the first successful check's return. Describes uncertainty, not time spent computing. |
| `host_cuda_submission_s` | Preparing and submitting this frame's GPU operations from the CPU. Starts when the worker enters the adapter's launch method and ends after it submits the final ready event. Includes host allocation/copy work and CUDA API calls. The GPU may execute earlier operations while the CPU is still submitting later ones. |
| `preprocess_s` | Preparing the image for the model on the GPU. Measures from the preprocessing-start event to the model-start event. Includes input upload on the non-graph path, image transformations and any stalls between those events. Excludes waiting before the GPU reaches the first event. |
| `inference_s` | Running the model on the GPU. Measures between the events placed before and after model execution. Includes any stalls between those events, not only active kernel computation. `cuda_event_ms` describes the same interval in milliseconds. |
| `gpu_output_transform_s` | Turning the model's GPU output into the values needed by the CPU. Measures from the model-end event to the output-transform-end event. For segmentation this includes resizing and selecting pixel classes; for detection it includes preparing the output tensor. |
| `host_output_allocation_s` | Allocating pinned CPU memory to receive the GPU result. Measures the CPU allocation call. GPU work can continue during this allocation. |
| `gpu_output_to_readback_s` | Time between the GPU finishing output transformations and reaching the marker immediately before the copy to CPU memory. Can include the stream sitting idle while the CPU allocates the destination and submits the copy. |
| `gpu_readback_s` | Copying the prepared GPU output to CPU memory. Measures from the event immediately before the copy to the final ready event after it. Includes stalls or submission gaps within that interval; it is not a direct measure of transfer bandwidth. |
| `cuda_pipeline_s` | The entire CUDA interval for the frame, measured once from the preprocessing-start event to the final ready event. Includes upload where applicable, preprocessing, inference, output transformations, copying output to CPU memory, and stalls between those operations. Contains the individual GPU stages above. |
| `cuda_submission_to_completion_observed_s` | CPU elapsed time from asking CUDA to record the preprocessing-start marker to observing that the final ready marker is complete. At that point the output is available in CPU memory. Includes submission overhead, time before GPU execution, the GPU stages, and delay before the observer notices completion. |
| `stream_waiting_est_s` | Estimates time outside the frame's measured CUDA interval: subtracts `cuda_pipeline_s` from `cuda_submission_to_completion_observed_s`. Includes waiting before the start marker, enqueue overhead and final-completion observation delay. Does not isolate CUDA queue waiting. |
| `launch_to_completion_observed_s` | Waiting on the CPU side after all of the frame's CUDA work has been submitted. Starts at `gpu_launch_end_ns` and ends at `gpu_completion_observed_ns`, when the observer sees the final ready event complete. Includes GPU work still outstanding and observation delay. |
| `completion_executor_wait_s` | Waiting for a CPU completion thread to pick up the finished GPU result. Starts at `completion_enqueued_ns`, just before submitting that task to the thread pool, and ends at `completion_executor_start_ns`, when its function begins running. |
| `finish_ready_check_s` | Confirming in the completion thread that the GPU output is ready to use. Measures the `ready.synchronize()` call; the observer normally already confirmed completion, so it should return promptly. The synchronous profiling path can wait here. |
| `cpu_format_s` | Turning the CPU-accessible model output into Python result objects. Starts before obtaining the output's NumPy view and ends after building detection dictionaries or the segmentation payload. Includes segmentation run-length encoding. |
| `cpu_format_thread_s` | CPU execution time used by the completion thread while formatting that result. Unlike `cpu_format_s`, it does not count time when this thread is blocked or not running. |
| `rle_s` | Encoding the segmentation label map as runs of equal class labels. Starts before calling the RLE function and ends when it returns the Python lists. Includes conversion to uint16, finding run boundaries, and creating class/length integers and lists. This is part of CPU formatting. |
| `rle_thread_s` | CPU execution time used by the completion thread during run-length encoding. Compare with `rle_s` to distinguish CPU execution from elapsed time when the thread was not running; the difference does not identify why it was not running. |
| `rle_run_count` | Number of class/length pairs produced by run-length encoding. This is a count, not a duration. More runs require more Python result objects. |
| `postprocess_s` | Combined time for handling the model output: GPU model-end → final-ready duration, plus CPU elapsed time in `finish()` after its readiness check through result formatting and timing bookkeeping. Includes GPU transformations, copying output to CPU memory and CPU formatting. Excludes waiting for a completion thread. |
| `processing_s` | Overall elapsed time from entering the worker's launch method through completing result formatting and timing bookkeeping. Includes host submission, GPU execution/waiting, observation delay, waiting for a completion thread and CPU formatting. Ends before sending the result back to the engine. |
| `worker_result_handoff_s` | Returning the worker's result to the engine process. Starts at the worker-finish timestamp, just before outgoing queue submission, and ends when the main process retrieves and deserializes the result. Includes serialization, process communication, queue waiting and engine polling delay. |
| `engine_result_polled_ns` → `result_ready_monotonic_ns` | Handling the returned result in the engine until it is ready for delivery. Tracking results also wait for their frame order and update the tracker during this interval. |
| `reorder_wait_s` | Holding a tracking frame's detections until the engine begins its tracker update. Uses the recorded detection-ready and tracker-start timestamps. Other frames or engine work can delay this step. |
| `tracker_s` | Updating the tracking state for this frame. Measures elapsed time around the tracker call, including any wait within that call. |
| `result_prepare_s` | Asking a background thread to prepare the UE-facing JSON text and waiting for the engine coroutine to receive it. Includes waiting for the thread, JSON preparation and waiting for the coroutine to resume. |
| `serialization_s` | Preparing the UE-facing dictionary, converting it to JSON text, and constructing the reusable serialized-result object inside the background thread. Measures from `serialization_start_ns` to `serialization_end_ns`. |
| `serialization_thread_s` | CPU execution time used by that thread while preparing the serialized result. Does not include time the thread is blocked or not scheduled. |
| `result_prepare_start_monotonic_ns` → `serialization_start_ns` | Waiting for the background thread to begin preparing the JSON result after the engine requests it. |
| `serialization_end_ns` → `result_prepare_end_monotonic_ns` | Waiting for the engine coroutine to resume after the background thread has prepared the serialized result and recorded its execution timing. |
| `outbound_queue_s` | Waiting in the UE connection's outgoing-message queue. Starts when the result is enqueued and ends when the sender retrieves it. Other messages and event-loop scheduling can delay retrieval. |
| `websocket_send_s` | Handing the serialized text to the WebSocket connection. Measures `ws.send_text()` from call to return, including any backpressure or scheduling delay. Return does not prove the UE application has received the result. |
| `replay_queue_s` | Waiting to start saving the serialized result for reconnect replay. Starts before reserving writer capacity and ends when the replay writer retrieves the queued item. Includes lock/capacity-reservation work and queue waiting. |
| `replay_write_s` | Writing the serialized result to SQLite and committing it. Starts when the writer retrieves the item and ends after the commit returns; includes opening the connection if needed and write bookkeeping. Socket delivery proceeds independently. |
| `gc_overlap_s`, `gc_overlap_count` | Time and number of process-wide garbage collections overlapping CPU result formatting, when GC tracing is enabled. They show overlap, not proof that garbage collection caused the frame's delay. |
| `MEC_TRACE_HANDOFF=1` | Enables more detailed timings for serializing/deserializing worker jobs, pipe reads/writes, and CPU calls that submit CUDA work. A pipe read can include waiting for a job that has not arrived yet. These intervals can overlap; see the queue document before combining them. |

### Stream waiting estimate

The calculation is:

```text
cuda_submission_to_completion_observed_s =
    (gpu_completion_observed_ns - cuda_preprocess_submit_start_ns) / 1e9
cuda_pipeline_s = preprocess_start.elapsed_time(ready) / 1000
stream_waiting_est_s = cuda_submission_to_completion_observed_s - cuda_pipeline_s
```

`cuda_preprocess_submit_start_ns` is recorded on the CPU immediately before asking
CUDA to put the preprocessing-start marker in the stream. `gpu_completion_observed_ns`
is recorded on the CPU immediately after the observer first sees the final ready
marker complete. Both timestamps use the host monotonic clock.

The CUDA duration starts when the GPU reaches the preprocessing-start marker and
ends when it reaches the final ready marker after copying the output to CPU
memory. Only durations in seconds are subtracted; no absolute CPU/GPU timestamps
are mixed. The CUDA duration includes stalls between its events and is measured
directly, not assembled from individual stage totals.

The completion thread calculates the estimate using the observer's already
recorded timestamp. Waiting for that thread and its CPU formatting, RLE, JSON
serialization and result delivery do not enter the estimate. Do not subtract CPU
submission time again: submission can happen while the GPU is already executing.

The estimate includes time before the start marker executes and time after the
final marker completes but before the CPU notices. The observer starts checking a
frame after the worker finishes submitting its CUDA work. Polling intervals,
Python's interpreter lock (GIL), and CPU scheduling can therefore increase the
estimate. It cannot identify how much delay came from earlier frames, GPU
scheduling, host enqueue overhead, or late observation.

Small negative estimates can result from timing resolution and clock uncertainty.
Keep them rather than replacing them with zero. Large negative estimates require
investigating measurement quality; they are not physical negative waiting.
Reports allow negative values for this estimate only. Without submission and
observer timestamps, the estimate is unavailable. No additional synchronization,
CUDA event or Nsight collection is needed.

### Worker transfer and preprocessing-start bounds

`worker_process_transfer_s` is the field written by the engine. The analyzer and
SQLite exporter also accept the legacy name `queue_wait_s`; an explicit null in
`worker_process_transfer_s` stays unavailable. Reports use
`mec_worker_process_transfer`, and the export's `frames` column is
`worker_process_transfer_s`. The plotter accepts legacy CSVs containing
`mec_queue_wait`. Source logs and databases are not rewritten. Rerun exports to
produce the renamed column. Scenario and time-window selection are unchanged.

The worker records `worker_start_monotonic_ns` immediately after retrieving the
job, before accessing its shared image. It then records
`cuda_preprocess_submit_start_ns` and `cuda_preprocess_submit_end_ns` immediately
before and after calling `record(stream)` on the preprocessing-start event. This
call adds a marker; it does not submit the entire frame as one operation or wait
for preprocessing to start. `gpu_submit_ns` refers to a later point: immediately
before recording the model-start event, after preprocessing has been submitted.

The observer checks the preprocessing-start event without waiting for it. It stops
checking once a query reports completion. The bounds use these CPU timestamps:

| Symbol | Timestamp | What happens at this point |
|---|---|---|
| S0 | `cuda_preprocess_submit_start_ns` | The worker is about to ask CUDA to record the preprocessing-start marker. |
| S1 | `cuda_preprocess_submit_end_ns` | That CUDA call has returned to the worker. |
| L | `preprocess_marker_last_not_ready_ns`, or S0 if no query reported incomplete | The observer is about to make its last check that finds the marker incomplete. |
| U | `preprocess_marker_observed_ns` | The observer has just made its first check that finds the marker complete. |

The actual marker submission occurs between S0 and S1. Its completion is bounded
by L and U. In seconds:

```text
cuda_submit_to_preprocess_marker_lower_s = max(0, L - S1) / 1e9
cuda_submit_to_preprocess_marker_upper_s = (U - S0) / 1e9
preprocess_marker_observation_span_s = (U - L) / 1e9
```

L uses query entry rather than return because the GPU could complete the marker
while the CPU is returning from an unsuccessful query. A zero lower bound means
we cannot establish a positive minimum wait; it does not establish zero waiting.

Late observation makes the bounds wider. Also, reaching the marker does not prove
that the next preprocessing kernel has started: the CPU may still be submitting
it, or the GPU may not have scheduled it. The bounds do not measure that gap.
This follows the [CUDA event-query semantics](https://docs.nvidia.com/cuda/cuda-runtime-api/cuda_runtime_api/group__CUDART__EVENT.html).
Queries do not synchronize the pipeline, but their CPU/API overhead should be
checked when validating performance. Mock workers, synchronous profiling calls
without observer evidence, and logs without these measurements leave the bounds
unavailable.

### Where the measurements are recorded

Stage data stays in MEC records, never in UE result JSON. `jobs.jsonl` contains
timings known when the result is prepared for delivery. Successful `send_attempt`
events contain outgoing-queue and WebSocket-send timings. `result_persisted` events
contain replay-writer and commit timings after the database write completes.
Both event types contain the result ID and the campaign/scenario/attempt identity
captured for that result; persistence also retains run/video/frame identity.
Join using `(run_id, campaign_id, scenario_id, attempt_id, result_id)`, not file order.
The analyzer checks the selected run and uses the earliest successful send's
measurements, so replay sends do not replace the original delivery measurement.
A failed or cancelled send/write has no successful-completion duration.

`MEC_TRACE_GC=1` enables a worker-process callback that remembers up to 2,048 garbage
collection intervals. It compares those intervals with the time spent formatting
each result. Two overlapping frames can both count the same collection. If the
buffer has discarded intervals needed for a frame, `gc_overlap_available=false`
and the duration is omitted. Disabled tracing also omits these fields. Overlap is
**correlation, not proof that garbage collection caused a pause**.

Wall time minus thread CPU time combines blocking, waiting for the GIL and time
when the OS did not schedule the thread. It cannot distinguish those causes.
Python memory allocation, reference-count cleanup, and native-library work on
other threads are not measured separately. The GC callback does not write a log
record for every frame.

### What these measurements cannot separate

The timings do not individually measure GStreamer's jitter buffering, packet
assembly for decoding, parsing, decoding and RGB conversion. They do not isolate
exact CUDA waiting before preprocessing, delay from physical GPU completion to
CPU observation, pure transfer time between processes, or pure GPU-to-CPU copy
time without submission gaps. Shared-memory lock waiting, allocation and copying
are combined. GIL waiting, OS scheduling, Python allocation and reference-count
cleanup are not individually attributed. Recorder queue/disk latency and time in
the socket/network/UE receive path are also outside these measurements.

Optional receiver diagnostics and worker handoff tracing can narrow some gaps,
but these measurements alone cannot assign the campaign's approximately 174.53 ms
residual to one cause. A CPU-side segmentation spike does not by itself establish
that RLE or garbage collection caused the delay.

After separate approval to run scenarios, validate one existing single-UE and
one existing four-UE scenario with the same model/media/settings and normal
campaign controller. Use new evidence directories, preserve the original pilot.
Check unique terminal outcomes, no overflow, unchanged payloads, successful drain,
and utilization fields with shared-system total/available memory explicitly labeled. Check segmentation
run counts, nonnegative timings, ordering of host boundaries, and late-event joins
for every successfully sent/persisted result. Ensure socket delivery still
proceeds while persistence is pending. Analyze and plot each approved campaign:

```bash
python tools/campaign_analyze.py --campaign-dir runs/campaigns/VALIDATION_NAME
python tools/campaign_plot.py --campaign-dir runs/campaigns/VALIDATION_NAME
```

The plotter imports the analyzer's stage registry, so new metrics automatically
produce CDFs; old logs yield missing samples and skipped plots. Valid-attempt
selection, arrival window **[1 s, 6 s)** and successful MEC send **<=6 s** remain
unchanged. Inspect CPU wall/thread time, RLE run count and optional GC overlap
for segmentation tails, then executor/handoff/serialization/send/persistence
timings. Compare measured overhead and latency distributions before deploying
tracing broadly. CUDA measurements require validation on MEC; local mock tests
cannot establish hardware timing accuracy.


### Local validation evidence

Focused CPU suite: 47 passed, 4 skipped (hardware/GStreamer dependencies).
Optional handoff/GC trace suite: 9 passed, 1 CUDA test skipped. The additional
CUDA-event arithmetic and analyzer tests passed after separating the readback
submission boundary. Compilation and diff whitespace checks passed. The separate
existing WebSocket integration test fails before startup: development settings
specify FPS/GoP 30 but the referenced catalog specifies 10; these files were not
changed. No CUDA or GStreamer hardware acceptance claim is made.

Reanalysis of the original pilot into a temporary directory preserved every
original group metric and the selection counts: 1,981 selected, 1,998 arrivals
in-window, 17 sent after cutoff, 798 outside-window. The original evidence and
reports were not overwritten. Reported p99 values match the user-provided figures.
