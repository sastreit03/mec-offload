# Aetheredge architecture fixes

Updated October 26, 2026. This note summarizes the remediation work following
the review in `/tmp/mec-architecture-review.md`. It covers `multi-ue/`,
`EdgeRIC-v2/muApp_mec/`, and the Aetheredge additions to OAI. It describes
implemented software behavior; over-the-air correctness and performance still
require live validation.

## Terms

- **Plan/configuration**: a versioned MEC decision selecting bitrate, model, and
  its future activation boundary.
- **Demand ledger**: EdgeRIC's per-frame record of total, confirmed, estimated,
  and remaining bytes.
- **Authoritative synchronization**: a full MEC snapshot that replaces stale
  EdgeRIC session and frame state after messages have been lost.

## Numbered review findings

1. **All-suspended resumption:** The GoP coordinator now evaluates suspended
   sessions even when no session remains active. It resumes sessions oldest
   first only when current radio evidence and the joint radio/compute budget
   permit them.

2. **Suspension timing and radio demand:** Suspension is scheduled at the next
   phased GoP boundary after the configured lead interval. Versioned
   transmission-control state is sent to both the UE and EdgeRIC. EdgeRIC stops
   generating demand after that boundary but retains bytes for frames that were
   actually transmitted. Resume commands carry the preceding stop interval so
   reordered messages cannot undo a suspension.

3. **Unseen tracking predecessors:** The proposed bounded wait for a frame whose
   descriptor has never arrived was not added. The tracker still advances by
   the observed frame-number gap, which produces the same eventual tracker age
   but can do so earlier than the reviewed specification describes. This was an
   accepted simplification.

4. **Tracking across suspension:** Linux and Android now consume source frames
   and advance frame IDs while transmission is suspended. On resume, the frame
   number therefore exposes the elapsed original-video time. ByteTrack ages its
   retained tracks across that gap, and the MEC records the skipped IDs as
   suspension drops.

5. **Recovery synchronization:** MEC awareness messages carry ordered revision
   numbers. A full synchronization is authoritative: it removes sessions and
   actual frame demand absent from the snapshot, rejects older queued updates,
   and preserves inference failures separately from radio reception state.

6. **Reusable session capacity:** A closed EdgeRIC session is removed once all
   of its frame demand has completed. A compact identity/revision tombstone
   rejects delayed messages without retaining the full ledger, allowing the
   configured session capacity to be reused.

7. **Initial and pending demand:** EdgeRIC now keeps pending and applied plans
   separately. It creates provisional frame demand at an advertised future
   activation boundary, replaces predictions with actual descriptors without
   losing credited service, and retracts untouched predictions when a plan is
   superseded or transmission stops.

8. **Out-of-order plan metadata:** MEC and EdgeRIC compare plan and
   configuration versions before advancing current state. A delayed old frame
   keeps its original configuration but cannot roll the session back. Ordered
   evidence beyond a pending boundary records that the planned switch was not
   applied.

9. **Terminal lifecycle:** A repeated or newer `video_end` command can no longer
   move a completed, expired, or denied video back into the draining state.

10. **Late Linux activation:** The Linux media loop checks a command boundary
    against the current mapped clock after capture or encoding stalls. It
    reports a missed boundary and keeps the prior configuration instead of
    applying a late change.

## Other conformance work

- **Execution failures:** A failed model quarantines every bitrate candidate
  backed by the same application/model/input/precision artifact. Newly decoded
  frames cannot enter a quarantined candidate. Automatic worker restart and
  unquarantine remain deferred.
- **Configuration:** FPS, GoP frame count, enabled applications, and one to
  three priority streams are validated configuration values. Keyframe
  interleaving is configurable for ablation tests and remains enabled by
  default. Defaults preserve the original 30 FPS, 30-frame GoP, three
  applications, and three priority streams.
- **Decision and admission evidence:** GoP bitrate/DNN decisions and per-frame
  priority decisions have separate start, end, and latency fields. Every
  accepted or rejected start records one admission decision and reason.
  `evaluate_ue.py` now reports admission rate and failure totals grouped by
  reason, while retaining all transmitted frames in its compliance denominator.
- **Frame types:** The wire contract accepts only type `1` for an IDR keyframe
  and type `3` for a P frame. Non-IDR I frames and B frames are rejected by both
  clients and the Python contract.
- **Configured resource scope:** The aggregate byte-budget admission extension
  remains intentionally deferred. Existing per-video frame/byte limits and the
  joint profiled radio/compute check remain active.

## OAI-to-EdgeRIC ZMQ telemetry

Live radio telemetry no longer depends on scheduler CSV files. OAI now publishes
the following data through the existing ZeroMQ protobuf connection:

- `capacity_bytes_per_prb`, calculated with OAI's TBS function at the smallest
  legal configured new-data grant;
- the MCS, modulation, code rate, layers, symbol allocation, DMRS overhead, and
  reference TBS inputs needed to validate that capacity calculation;
- cumulative new-data and retransmission PRB/TBS counters, protected by a gNB
  process-generation identifier and report sequence number; and
- a snapshot of the latest committed grant for spot validation.

Cumulative counters let EdgeRIC recover the full delta when ZeroMQ keeps only a
newer report. EdgeRIC applies new-data TBS deltas to the oldest outstanding
frames for the corresponding RNTI. Retransmissions are recorded but do not
reduce estimated demand.

Between MEC progress reports, remaining frame demand is calculated as:

```text
remaining = max(0, total_encoded_bytes - max(confirmed_bytes, estimated_bytes))
```

`confirmed_bytes` is cumulative unique data observed by the MEC.
`estimated_bytes` is provisional service inferred from new-data grants. Taking
the maximum prevents confirmed and estimated observations of the same bytes
from being counted twice. The current payload conversion factor is `1.0`; live
measurements must determine whether protocol overhead or unrelated UE traffic
requires a lower value.

## Validation and deferred work

- The current Python suite has 72 passing tests, including focused lifecycle,
  synchronization, suspension, configuration, execution-failure, evaluation,
  ZMQ counter, playlist, and successor-handoff tests. The EdgeRIC C++ bridge
  passes syntax compilation.
- A deployed single-UE detection run validated live ZMQ association, capacity
  telemetry, RTP transport, model execution, result return, and video closure.
  OAI now reports raw newly received UL bytes for association, the MEC accepts
  an asynchronously starting RTP receiver, the validated jitter setting is
  100 ms, and the decoded-frame pending limit is 16.
- The 100-ms run returned all 1,217 outcomes, completed inference for 1,167
  frames, and met the 150-ms end-to-end SLO for 602 frames (49.47%). Deployment
  image IDs, exact configuration copies, and model hashes were not captured.
- Live tests must compare reference capacity with committed grants and compare
  new-data TBS deltas with later MEC unique-byte progress. The latest-grant
  snapshot supports sampling rather than an exhaustive record of every grant.
- Strict slot-k/slot-k+1 action timing and remote reinforcement learning remain
  accepted exclusions.
- Automatic failed-worker restart/unquarantine and the aggregate byte-budget
  admission extension remain deferred.
