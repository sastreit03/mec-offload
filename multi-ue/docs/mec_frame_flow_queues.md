# MEC frame flow: queues, accounting, and logging

This describes the current code as audited on 2026-11-21, from creation of a decoded RGB picture to submission to a worker process. It also explains the earlier packet accounting that this path depends on. For image copies and the later result-delivery path, see [mec_frame_flow_memory.md](mec_frame_flow_memory.md).

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
