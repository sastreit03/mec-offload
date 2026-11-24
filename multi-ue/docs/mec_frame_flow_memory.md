# MEC frame flow and memory

## Current memory handling (24 November 2026)

This section describes the current implementation. The **Memory audit** below is retained as the 20 November record. Its descriptions of two appsink image copies, whole-image worker IPC, engine application/priority deques, and synchronous inference no longer describe the current input path. Use this section for current memory ownership and copying behavior.

Timing fields and their boundaries are documented in [MEC frame timing](mec_frame_timing.md). Resource telemetry is documented in [NVML diagnosis](mec_nvml_diagnosis.md). Queue ownership and limits are described in [MEC frame flow queues](mec_frame_flow_queues.md).

### Picture ownership from decoder to worker

GStreamer decodes and converts the picture to RGB → the appsink callback retrieves the available sample → a NumPy view reads the mapped GStreamer buffer → **one pixel copy writes the picture into a shared-memory slot** → the receiver handoff and engine pass a lease protecting that slot → **worker IPC carries a descriptor, not the pixels** → the worker accesses the same slot through its own mapping.

Retrieving a sample with `pull-sample` takes it from appsink's internal queue into the callback. It does not send the picture to another process or duplicate its pixels. The callback maps the sample's buffer, constructs a NumPy view using the image's row stride and offset, and copies the visible RGB pixels into the receiver's `RGBPool`. It then drops the view and unmaps the GStreamer buffer. There is no intermediate Python `bytes` copy of the decoded picture. Compressed-packet inspection still copies packet bytes separately.

An `RGBLease` is a local ownership reference: while any lease remains, the pool cannot overwrite that slot. The ordered receiver handoff holds the decoded notification and its lease. During validation the engine temporarily attaches the lease to the frame. On accepted submission, `Workers.submit` retains a lease and the engine clears the frame's RGB reference; the receiver's lease is released when decoded-event handling finishes. The worker input can therefore remain valid without keeping the RGB reference in the engine frame until inference completes.

The production worker job contains a `shared_rgb` descriptor with the region name, slot, generation, offset, shape, row stride, and slot size. The multiprocessing queue serializes that metadata. The worker checks the descriptor's bounds and generation and exposes a read-only NumPy view of the shared pixels. Opening that mapping does not reconstruct another full picture. Alternate array-based inputs used outside this receiver path should not be assumed to have the same copy behavior.

There is one worker input queue per application and a CUDA stream per priority. The engine no longer holds the earlier per-application/per-priority dispatch deques. Workers submit CUDA work asynchronously, so several frames may be outstanding; ownership is tracked per job rather than by a single busy slot lasting until engine completion.

Sources: [media.py](../mec/src/mec_server/media.py), [shared_rgb.py](../mec/src/mec_server/shared_rgb.py), [engine.py](../mec/src/mec_server/engine.py), and [workers.py](../mec/src/mec_server/workers.py).

### CUDA graph input and inference storage

The TorchScript configuration defaults to `cuda_graphs=true`. With graphs enabled, the worker registers the receiver's shared-memory region with CUDA so a GPU kernel can read its pixels directly. Registration is prepared before frame processing and retained across frames; it is not a full-image upload for each job.

The preprocessing kernel reads the shared RGB slot and writes resized, padded, normalized FP16 input into a fixed CUDA tensor. Each model/priority pair has its own captured graph and fixed input/output storage, prepared during worker startup. Graph replay runs the model after preprocessing on the same priority stream. Output transformations and readback are queued before that stream processes the next frame, protecting reused graph output from being overwritten before it is consumed.

This path has no additional full RGB copy into a pinned staging tensor and no explicit per-frame RGB upload. It still writes a separate model-input tensor and uses model weights, graph storage, intermediate tensors, output transformations, and CPU result buffers. Shared physical RAM on the Spark does not eliminate those logical allocations or make the entire pipeline copy-free.

The worker records an `input_read_done` CUDA event after the preprocessing kernel. Once its observer sees that event complete, it drops the job's input mapping and sends an `input_released` acknowledgement. The parent releases the corresponding worker lease when it receives the acknowledgement. Thus the RGB slot can become reusable before inference finishes, provided preview or another owner does not still hold a lease. Full job completion also provides a fallback release path. A failed worker's leases are released only after its process is confirmed to have exited; a deadline alone does not make GPU input safe to overwrite.

The registration cache tracks active readers separately from the pool's leases. Releasing a video retires its registration, and removal waits for active readers to drain. Closing a receiver pool similarly defers closing and unlinking the shared region until its leases drain. Releasing a slot makes it reusable; it does not deallocate the whole region.

Sources: [cuda_rgb.py](../mec/src/mec_server/cuda_rgb.py), [workers.py](../mec/src/mec_server/workers.py), and [shared_rgb.py](../mec/src/mec_server/shared_rgb.py).

### Implemented path with CUDA graphs disabled

The non-graph path is also implemented. It reads the shared-memory picture into a separate pinned CPU tensor with `np.copyto`, then submits a nonblocking transfer to a CUDA tensor. Tensor operations perform preprocessing before ordinary model execution. The shared RGB lease can be released after the CPU copy; the pinned tensor remains referenced through GPU completion so the asynchronous upload can safely finish.

This alternative therefore has an additional full RGB CPU copy and an explicit upload. Its allocations should not be used to describe the graph-enabled direct-input path. Both paths use asynchronous submission and completion observation during normal frame processing; the synchronous `infer` helper used for warmup/profiling is not the normal frame path.

### Output, preview, and result lifetimes

After model execution, GPU operations produce detection output or a segmentation label map. The worker allocates pinned CPU output storage and submits a nonblocking device-to-host copy. CPU postprocessing starts after completion is observed. It reads that output through NumPy and constructs result objects. Segmentation additionally creates converted label data, run boundaries, and many Python lists representing RLE runs. These objects can occupy substantially more space than the compact label array.

The worker completion queue serializes the result objects back to the engine process; this return path does reconstruct result objects. It does not carry the RGB picture. Engine frame state and, where applicable, tracking order can retain results until delivery.

Preview has its own lease on the latest pending picture when viewers are present. Replacing a pending preview releases its lease. Encoding reads the shared picture and allocates resized/color-converted images and JPEG bytes; the lease is released after the encoder thread finishes. Preview can therefore keep a slot occupied after inference has stopped reading it. Without viewers, this branch does not retain a picture for JPEG encoding.

Delivery still serializes the UE result to JSON once, excluding stage timings. The outgoing WebSocket queue and asynchronous replay writer reference the same serialized result. Logging copies payload-free metadata, rather than duplicating segmentation payloads into evidence records. Original Python result objects may remain referenced by delivery code until its work finishes; serialization does not guarantee immediate reclamation. SQLite, WebSocket libraries, and the operating system may make their own internal buffers. Scenario completion and reconnect replay still wait for the required persistence barrier.

Sources: [preview.py](../mec/src/mec_server/preview.py), [results.py](../mec/src/mec_server/results.py), [recording.py](../mec/src/mec_server/recording.py), and [app.py](../mec/src/mec_server/app.py).

### Explicit pixel copies and retained storage

| Boundary | Memory operation | What keeps the storage alive |
| --- | --- | --- |
| Appsink sample to NumPy view | View of mapped GStreamer pixels; no full pixel copy | Sample and buffer mapping during callback |
| NumPy view to RGB pool | One copy into a shared-memory slot | Receiver, worker, and optional preview leases |
| Engine to worker | Descriptor serialization; shared pixels are mapped | Parent worker lease until input acknowledgement or safe fallback release |
| Shared RGB to graph input | GPU kernel reads mapped RGB and writes fixed FP16 CUDA input | RGB lease until preprocessing read completes; graph input retained across frames |
| Shared RGB to non-graph input | CPU copy into pinned storage, then asynchronous CUDA upload | Pinned tensor retained through GPU completion |
| Model output to CPU | GPU transformations and asynchronous copy into pinned output | In-flight submission and CPU completion handling |
| CPU output to result | NumPy processing and Python result allocation, including segmentation RLE | Worker completion, then engine/delivery references |
| Worker result to engine | Binary serialization reconstructs result objects | Engine frame and delivery state |
| Result to socket/replay | One JSON serialization shared by both paths | Outbound queue and asynchronous persistence operation |

Each receiver has a pool with `shared_rgb_slots` slots (default 32). Each slot reserves `decoded_max_bytes` (default 8 MiB), plus a 64-byte header, rounded up to a 4,096-byte boundary. Reserved capacity is therefore `slots × ceil((decoded_max_bytes + 64) / 4096) × 4096`: about 256.125 MiB per receiver with those defaults, even when a 1280 × 720 RGB picture uses only 2,764,800 pixel bytes. The pool reserves its shared-memory backing before media starts, so insufficient backing storage fails at allocation rather than during a later pixel write.

Pool statistics distinguish occupied slots, lease references, total capacity, peak occupancy, copies, and exhaustion. Multiple leases can refer to one occupied slot. Pool exhaustion fails the frame rather than overwriting a slot in use. This bound covers shared decoded RGB storage only: GStreamer buffers, preview allocations, pinned buffers, CUDA graph/model storage, result objects, queued JSON, and library caches are separate. Python and PyTorch may retain freed allocation capacity for reuse instead of immediately returning RAM to the operating system.

---

## Memory audit

Produced on 20 November 2026. This describes the current TorchScript processing path, from received video to the result sent to a UE. A **picture** is a decoded RGB pixel array: a 1280 × 720 picture occupies 2,764,800 bytes (about 2.64 MiB), before object overhead. A **reference** keeps an existing object alive without duplicating its contents. **Serialization** turns an object into bytes or text for transfer or storage.

## Through-line

GStreamer decodes video into RGB → **two image copies at appsink** produce an independent NumPy picture → **references pass through the event queue and frame record**, while the application queue holds frame identifiers → **binary serialization to the worker process** reconstructs a separate picture → **another CPU image copy and CUDA tensor allocation** prepare inference → **postprocessing creates result objects** → **binary serialization back to the main process** reconstructs those results → **references pass into the outgoing result message**, and the engine releases its picture reference → **one JSON serialization excludes stage timings** → **a payload-free evidence record is deep-copied and logged**, including serialization duration → **the shared SQLite writer and per-UE send queue reference the same JSON string** → **WebSocket delivery proceeds without waiting for the SQLite commit**. Scenario completion and reconnect replay still wait for the required writes.

The result-delivery changes below were implemented on 2026-11-20. Previous behavior is retained explicitly for comparison. This describes repository code; running deployments must be rebuilt/restarted to use it.

## Detailed path

### 1. Received packets become a decoded picture

Each video has a GStreamer pipeline:

```text
UDP → RTP jitter buffer → H.264 depayloader → H.264 parser
    → decoder → RGB conversion → appsink
```

The operating system and GStreamer buffer compressed packets. The decoder retains decoded pictures and reference pictures needed for later P frames. `videoconvert` produces RGB pixels.

Separately, our packet-inspection callbacks use `extract_dup()` at the UDP input and jitter-buffer output, copying packet bytes into Python. The assembly checker retains payloads while checking whether the encoded frame is complete; it does not supply the decoder's input. Completion or cleanup releases the assembly's references.

Exact allocations inside GStreamer and the operating system are implementation-dependent; this document identifies the explicit operations in our code. See [media.py](../mec/src/mec_server/media.py), `Receiver.start`, `_packet`, and `_jitter`.

### 2. Appsink hands the picture to Python

Appsink has `max-buffers=1`, `drop=false`, and `sync=false`. It queues one decoded sample internally. `pull-sample` removes that sample without making a full pixel copy; the callback still accesses GStreamer's buffer.

The callback makes **two image copies at appsink**:

1. `buf.extract_dup(0, size)` copies the RGB buffer into Python bytes.
2. `np.frombuffer`, reshaping, and slicing create views of those bytes; the final `.copy()` allocates an independent, contiguous NumPy RGB array.

The GStreamer buffer, Python bytes, and independent array can briefly coexist. The independent array survives after the callback returns and GStreamer releases or reuses its storage. `decoded_ns` is recorded after this array is prepared.

The one-buffer limit applies only inside appsink. Pictures already handed to Python can accumulate in later queues. See [media.py](../mec/src/mec_server/media.py), `Receiver._sample`.

### 3. The engine retains and queues the picture

**References pass through the event queue and frame record.** The callback posts a descriptor, RGB-array reference, and timestamp to `self.events`, a shared queue of up to 1,024 mixed events from all UEs and applications. This internal message is not a write to `events.jsonl`.

The engine handles the event and assigns `f.rgb = rgb` in the existing frame record. Neither operation copies pixels. It validates the frame/model, selects the priority, checks memory limits, and records `queue_enter_monotonic_ns`.

The application/priority FIFO stores only `(video_id, frame_id)`. The image remains in `self.frames`. UEs using the same application and priority share that FIFO; there is no separate per-UE processing queue.

If a dashboard preview is being watched, it also retains a reference to the latest picture. Preview encoding allocates resized/color-converted images and JPEG data in a thread. This optional branch can extend the picture's lifetime; without viewers, it does not encode JPEGs.

See [engine.py](../mec/src/mec_server/engine.py), `post` and `decoded`, and [preview.py](../mec/src/mec_server/preview.py).

### 4. Dispatch creates a worker-process copy

Building a job dictionary references the RGB array without copying it. **Binary serialization to the worker process** then serializes the job, including pixels, through a multiprocessing queue. The worker reconstructs its own NumPy array. This is not JSON and does not use an explicitly shared image-memory allocation.

The main-process picture, worker picture, and temporary serialization/transport buffers can coexist. The engine retains `f.rgb` until completion handling or another resolution path releases it.

Each application has a worker process and a CUDA stream per configured priority. There is one outstanding job per application/priority, shared across its UEs. Collection mode uses the first configured priority. The slot stays busy until the main engine polls the completion, not merely until GPU inference finishes.

See [workers.py](../mec/src/mec_server/workers.py), `Workers.submit`, `Workers.poll`, and `worker_main`.

### 5. The worker prepares input and runs inference

`torch.as_tensor(image.copy(), device="cuda")` makes **another CPU image copy and CUDA tensor allocation**. Unified physical memory on the DGX Spark does not make this code a zero-copy NumPy-to-CUDA view.

`permute` and `unsqueeze` create tensor views. Conversion to half precision, division by 255, resize, padding, and segmentation normalization produce transformed tensors and temporary storage. The worker waits for its stream's preprocessing to finish.

Model weights are already resident. Inference creates intermediate tensors and output, then the worker waits for inference completion. CPU image references can remain alive throughout the job. PyTorch may cache released CUDA storage for reuse rather than return it immediately to the system.

See [workers.py](../mec/src/mec_server/workers.py), `TorchAdapter.infer`.

### 6. Postprocessing creates the result

**Postprocessing creates result objects**, not a copy of the input picture:

- **Detection:** output is converted to float as needed and transferred to CPU. NumPy exposes the CPU tensor's storage. Python constructs detection dictionaries containing boxes, scores, and class IDs.
- **Segmentation:** CUDA operations resize outputs and select class labels. An int32 label map transfers to CPU and is exposed through NumPy. Run-length encoding converts labels to uint16 and constructs Python `[class, length]` lists, with temporary arrays for run boundaries. These lists have Python-object overhead in addition to their eventual JSON size.

`processing_s` is a duration ending after postprocessing. It excludes transferring results back to the main process.

### 7. The result returns and the picture can be released

**Binary serialization back to the main process** transfers a completion dictionary containing the payload and timings, but no RGB image. The main process reconstructs separate result objects.

When the engine polls completion, it releases the processing slot, attaches the payload to the frame by reference, and sets `f.rgb = None`. This removes one image reference; previews or other remaining references can still keep the array alive. Worker-local objects likewise become reclaimable only after their references disappear.

Detection and segmentation proceed directly to resolution. Tracking, when enabled, first holds detection results in a per-video reorder buffer and updates a persistent tracker in frame order. Its thread shares process memory, so this step does not require multiprocessing serialization.

**References pass into the outgoing result message.** Resolution builds a dictionary containing metadata, timings, outcome, and payload without deep-copying the payload. It removes the frame from the active frame/tracking maps and schedules delivery. See [engine.py](../mec/src/mec_server/engine.py), `tick`, `track`, and `resolve`.

### 8. Serialize the UE-facing result once

**Previous:** `deliver` serialized the complete message, including stage timings, discarded the JSON string, then added preparation timing fields to the dictionary. SQLite and the sender serialized that dictionary again.

**Current: one JSON serialization excludes stage timings.** `SerializedResult.prepare` constructs the UE-facing dictionary without `stage_times` and serializes it in a thread. The prepared result retains the JSON string plus read-only routing metadata, not a second Python payload tree. Identifiers, processing configuration, outcome, reason, and payload remain on the wire for compatibility. Both Android and Linux clients compute latency from their own timestamps and have no dependency on `stage_times`.

The original MEC evidence dictionary retains all stage timings. After serialization, `deliver` adds `result_prepare_s` and its start/end timestamps to that evidence only. This duration includes the thread handoff and preparation/serialization work. It is written to `jobs.jsonl`, never inserted back into the already serialized UE message. See [results.py](../mec/src/mec_server/results.py).

### 9. Evidence is copied and logged separately

**A payload-free evidence record is deep-copied and logged.** The engine excludes `payload` from the `jobs.jsonl` record. `Recorder.put` deep-copies the remaining metadata/timings into its logging queue; its writer thread serializes and writes the record.

Diagnostic `events.jsonl` records follow the same recorder mechanism. These records contain neither the RGB picture nor the full inference payload. See [recording.py](../mec/src/mec_server/recording.py), `Recorder`.

This evidence logging is unchanged: JSONL records still require their own serialization because they differ from the UE-facing message. The frame evidence now measures the serialization actually reused for delivery and replay storage.

### 10. One background SQLite writer serves all UEs

**Previous:** `ReplayStore.save` serialized the full message again, acquired a shared write lock, inserted the JSON, and committed. Delivery awaited that work before enqueueing the result for the UE.

**Current: the shared SQLite writer and per-UE send queue reference the same JSON string.** `ReplayStore.submit` reserves space and queues the string for one dedicated writer thread, with one connection owned by that thread. The writer inserts the string directly and commits; it does not JSON-serialize it again. SQLite still performs its own internal buffering and encoding.

`replay_pending_bytes` bounds serialized data waiting for or undergoing a write (default 64 MiB across all UEs; excludes Python/SQLite overhead). Overflow and write failures are explicit errors that invalidate collection. No queued result is silently discarded as successful. The writer's failure remains visible to subsequent submissions and replay.

Delivery enqueues the live result immediately after submitting persistence, then waits for its commit in the background. The existing per-video `deliveries` counter remains nonzero until that wait finishes, so finalization cannot report success with outstanding writes. Normal shutdown drains delivery tasks and closes/drains the writer. See [recording.py](../mec/src/mec_server/recording.py), `ReplayStore`, and [engine.py](../mec/src/mec_server/engine.py), `deliver` and `close`.

### 11. The per-UE sender serializes and transmits

**Previous:** the per-UE queue retained the full result dictionary; the sender performed a third full-result JSON serialization in a thread.

**Current: WebSocket delivery proceeds without waiting for the SQLite commit.** The per-UE queue retains the prepared result, including its existing JSON string and routing metadata. The sender passes that string directly to `ws.send_text`. There is no second or third JSON serialization for newly prepared frame results. Other control messages still serialize normally.

Each connection retains its own priority queue, with capacity 1,024 messages in collection mode. Control messages take priority over frame results; frame results follow enqueue order within their priority. The sender handles one message at a time. The writer and sender may finish in either order; neither needs the other's completion to proceed.

The WebSocket/network stack encodes text as bytes, adds protocol framing, and buffers data for TCP transmission. Exact internal copy counts depend on the libraries and operating system. A completed send call does not prove that the UE application has received the result.

After references disappear, temporary result objects and strings become reclaimable; the replay copy remains on disk. See [app.py](../mec/src/mec_server/app.py), the WebSocket `enqueue` and `sender` functions, and [engine.py](../mec/src/mec_server/engine.py), `deliver`.

### 12. Failure and reconnect paths

Invalid metadata, unrecoverable decoding, buffer limits, or worker failures can stop a frame before it completes this path. When the engine resolves a known frame as failed, its smaller failure message follows the delivery path without an inference payload. Not every rejected packet creates a frame result.

**Previous:** reconnect replay read a fixed database snapshot, reconstructed dictionaries, and serialized them again for sending.

**Current:** reconnect replay first captures a barrier for writes already submitted and waits for those commits before taking its database snapshot. Writes submitted afterward do not extend the barrier; their results use the current live connection. Persisted results retain stable result IDs, so UE deduplication handles overlaps between live delivery and replay.

Replay reads stored JSON and parses it for routing metadata, but sends the original string without reserializing it. A compatibility path strips stage timings and reserializes old database rows that contain them; new rows do not need this conversion. A persistence error aborts replay and invalidates collection. A full outgoing queue still closes the connection in collection mode.

Sending before commit intentionally allows a crash window: a UE may receive a result that has not yet reached durable storage. Clean shutdown and successful scenario finalization drain pending writes, but an abrupt process or machine failure cannot offer the previous guarantee that every sent result was already committed.
