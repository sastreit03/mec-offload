# Aetheredge implementation and validation record

Updated October 28, 2026. This record summarizes the code changes and checks
performed during the architecture review and initial testbed preparation. It
supplements `architecture-fixes.md`, which contains the shorter finding-by-
finding remediation summary. The authoritative design inputs remain the three
architecture documents in `docs/`.

## Scope and interpretation

The work covered the multi-UE MEC server and UE clients, the EdgeRIC MEC
application, and the Aetheredge additions to OAI. “Passed” below means the
named offline or source-level check passed. It does not establish over-the-air
correctness, sustained-load performance, model accuracy, or end-to-end SLO
compliance. Strict slot-k/slot-k+1 action timing and remote reinforcement
learning were accepted exclusions. Automatic failed-worker restart and
unquarantine, and aggregate byte-budget admission, remain deferred.

## Architecture review and remediation

The initial review compared `multi-ue/` and `EdgeRIC-v2/muApp_mec/` with MEC
architecture v2.1, Decision Maker architecture v4.4, and OAI/EdgeRIC
architecture v1.5. The committed baseline passed 35 Python tests but exposed
ten lifecycle, timing, recovery, and demand-accounting findings. The following
changes were made.

1. **All-suspended recovery.** The GoP coordinator now considers suspended
   sessions even when no active session remains. Resumption is ordered and is
   admitted only when radio evidence and the joint radio/compute budget allow
   it.
2. **Suspension timing.** Suspension is versioned and scheduled at a phased GoP
   boundary. Both the UE and EdgeRIC receive the transmission-control state.
   EdgeRIC stops predicting new demand at the boundary while retaining demand
   for packets that were actually sent.
3. **Tracking gaps.** UE frame IDs continue to advance while transmission is
   suspended. ByteTrack consequently sees the source-video gap after resume
   and ages tracks across it. A separate bounded wait for a descriptor that has
   never arrived was not added; observed frame-number gaps provide the eventual
   tracker advancement accepted for this implementation.
4. **Authoritative recovery.** MEC awareness messages carry revisions. A full
   synchronization removes stale sessions and frame demand that are absent
   from the snapshot and prevents older queued updates from restoring them.
5. **Reusable EdgeRIC capacity.** Completed sessions release their full demand
   ledgers. Compact tombstones retain enough identity and revision information
   to reject delayed messages without exhausting the configured session limit.
6. **Pending radio demand.** EdgeRIC holds pending and applied plans separately,
   predicts provisional demand at future activation boundaries, reconciles an
   actual descriptor without losing already credited service, and retracts
   untouched predictions when a plan is replaced or transmission stops.
7. **Ordered plan state.** Old descriptors retain their own configuration but
   cannot roll the current MEC or EdgeRIC session state backward. Ordered
   evidence after a boundary records a missed configuration switch.
8. **Terminal lifecycle.** Repeated or newer end commands cannot reopen a
   completed, expired, or denied video.
9. **Late activation.** The Linux UE compares a configuration boundary with the
   current mapped time after capture or encoding stalls. It reports a missed
   boundary and preserves the previous configuration rather than applying a
   late change.
10. **Execution-failure enforcement.** A failed model artifact quarantines all
    bitrate candidates that use it, and newly decoded frames cannot enter a
    quarantined candidate. Automatic worker recovery remains deferred.

## Configuration, evidence, and media contracts

- FPS, GoP frame count, enabled applications, and the number of priority
  streams are configurable. Defaults retain 30 FPS, a 30-frame GoP, three
  applications, and three priority streams.
- Keyframe interleaving is configurable for ablation experiments and enabled
  by default.
- GoP bitrate/model decisions and per-frame priority decisions have separate
  start, end, and latency fields. Every accepted or rejected start records an
  admission decision and reason. Evaluation reports admission rate and totals
  grouped by rejection reason.
- The media descriptor accepts only type `1` for an IDR keyframe and type `3`
  for a P frame. Non-IDR I frames and B frames are rejected.
- Brief comments and contract documentation were added around the changed
  lifecycle, demand, timing, and telemetry behavior.

## OAI-to-EdgeRIC ZMQ telemetry

Scheduler CSV files are no longer a runtime communication path between OAI and
EdgeRIC. The existing ZMQ protobuf report now carries the following data for
each relevant UE:

- `capacity_bytes_per_prb`, derived with OAI's existing transport-block-size
  calculation at the smallest legal configured new-data grant;
- the MCS, modulation order, code rate, layer count, symbol allocation, DMRS
  overhead, and reference PRB/TBS inputs needed to validate the derived value;
- cumulative new-data and retransmission PRB and TBS counters, with a gNB
  process-generation identifier and report sequence; and
- a snapshot of the latest committed grant for spot validation.

The scheduler performs one reference calculation per eligible UE and stores
compact in-memory telemetry before the ZMQ publisher serializes it. Cumulative
counters preserve the total delta when a latest-value ZMQ sample replaces an
older sample. EdgeRIC subtracts new-data TBS deltas from the oldest outstanding
demand for the associated RNTI. Retransmission telemetry is retained for
validation but does not reduce demand a second time.

Between MEC progress reports, EdgeRIC uses:

```text
remaining = max(0, total_encoded_bytes - max(confirmed_bytes, estimated_bytes))
```

`confirmed_bytes` is unique payload reported by the MEC. `estimated_bytes` is
provisional service inferred from OAI new-data grants. The maximum prevents the
same bytes from being credited twice. The present payload conversion factor is
1.0 and must be calibrated with live measurements if transport overhead or
unrelated traffic produces a material bias.

## Model preparation and profiling

- YOLO26 checkpoints and SegFormer B0–B3 checkpoints were downloaded outside
  Git and exported to TorchScript artifacts. The SegFormer configurations were
  checked to contain 19 `id2label` and 19 `label2id` entries, matching the
  Cityscapes-19 output heads.
- All twelve resident application/model copies were confirmed present: four
  YOLO variants for detection, four for tracking, and four SegFormer variants
  for segmentation.
- GPU access was verified inside the profiling container as `CUDA: True` on an
  NVIDIA GB10. CUDA 13.1 ran against a CUDA 13.0-capable driver in minor-version
  compatibility mode; this warning did not prevent the profiling run.
- NVIDIA MPS had to be started on the host with its control and log paths made
  visible in the container. Earlier runs correctly failed closed when MPS or
  CUDA was unavailable.
- A BDD100K sample was inspected as H.264, 720 by 1280, approximately 29.97
  FPS, 1,212 frames, 40.296667 seconds, 17,126,702 container bytes, and about
  3.40 Mbit/s.
- Profiling results are retained under `multi-ue/runs/profiling/`. The steady
  YOLO26x processing time used for configuration is approximately 12.0 ms for
  detection and 11.9 ms for tracking. Cold-start outliers remain in the raw
  CSV and are not treated as steady-state latency.
- YOLO26s was replaced by YOLO26x in the active task catalog, model export list,
  and synthetic profile entries. The temporary profiling catalog remains for
  reproducibility.
- The profiling catalog now contains the full 0.5, 1, 2, and 4 Mbit/s
  cross-product for every DNN and application (48 candidates). The final four
  bitrate/DNN pairs per application remain subject to measured quality results.

## Prerecorded-video UE continuity

Linux and Android UEs now support a directory of locally stored prerecorded
videos. Each UE shuffles the directory, sends every supported file once per
cycle, reshuffles for the next cycle, and avoids an immediate repeat across a
cycle boundary when more than one file exists. An optional seed makes the
selection reproducible, and selection/cycle information is logged.

Near the end of a prerecorded video, the UE asks the MEC to prepare a distinct
successor session. At end of input it queues `video_end` for the old video and
starts the prepared successor immediately, without waiting for the MEC's
completion response. The old and new videos have different identifiers,
receiver state, epochs, and trackers, so the MEC can reset per-video state.
The MEC safely promotes the successor whether the end control message or the
successor's first UDP descriptor arrives first. If pre-admission fails, the UE
uses ordinary admission for the already selected next file. An explicit user
stop disables continuation.

This continuity behavior applies only to prerecorded directory playback.
Single-file and camera streaming retain their prior lifecycle. Linux selects
the UE tunnel through its interface rather than a hard-coded `12.1.1.2`
address. Android binds to the selected cellular network and refreshes the
session binding when the network address changes.

## Tests and checks performed

| Check | Result | Meaning and limit |
|---|---:|---|
| Initial committed Python suite | 35 passed, 1 warning | Established the review baseline with mocked workers and local transports. |
| Post-remediation Python suite | 69 passed | Covered the ten findings, configuration, failure enforcement, evaluation, and ZMQ counter accounting. |
| Current full Python suite | 78 passed, 1 warning | Adds prerecorded playlist, successor handoff, fixed collection-mode controls, extended RAN telemetry, and labelled NVML device sampling. The warning is an unrelated Starlette test-client deprecation. |
| Focused continuity suite | 29 passed | Exercised lifecycle, playlist, and related control paths during implementation. |
| Python byte compilation | Passed | Changed Python files are syntactically importable. |
| Git whitespace/error check | Passed | `git diff --check` reports no malformed patch whitespace. |
| EdgeRIC C++ bridge syntax check | Passed | Validated the edited bridge source at compile-syntax level. |
| Android source API compilation before continuity work | Passed and recorded in `android-javac.txt` | Covered the earlier Android source against Android 35 APIs. |
| Android Gradle build after continuity work | Not run successfully on this host | Host Java 8 is too old for Android Gradle Plugin 8.7.3; compile on JDK 17 before deployment. Structural delimiter checks passed but are not a substitute for compilation. |
| Model artifact/configuration checks | Passed | Required artifacts were present and all SegFormer heads reported 19 labels. |
| Model profiling | Completed | Real GPU measurements were collected in the profiling CSV files; this is processing profiling, not end-to-end latency. |
| Full OAI build | Built and deployed | Includes the Aetheredge ZMQ capacity/grant telemetry and raw UL-byte association input. Image identity was not recorded. |
| Live gNB/EdgeRIC/MEC/UE run | Partial pass | One detection UE returned all 1,217 outcomes; 1,167 completed inference and 602 met the 150-ms SLO. Multi-UE, other applications, telemetry accuracy, and coexistence remain unverified. |

The subsequent telemetry extension adds explicit UL/DL MCS and BLER,
PUSCH/PUCCH SNR, latest and cumulative UL/DL PRB/TBS grants, frame/slot, and
RLC DL occupancy and direction-labelled MAC/RLC-interface totals to the versioned OAI ZMQ contract. EdgeRIC
persists C-RNTI only in its local evidence and derives 100-ms counter deltas.
MEC job records now carry candidate and bitrate directly, and MEC resource
events include whole-device NVML utilization alongside host-submitted queues.
These additions have unit/integration coverage but still require a rebuilt gNB
and live value/rate validation before experiment collection.

The current unit and integration suite covers RTP fragmentation, reordering and
duplicates; descriptor validation; control identity and replay fencing; demand
prediction and reconciliation; session retirement; authoritative recovery;
suspension/resumption; plan ordering; failure quarantine; admission evidence;
automatic UE/RNTI association; shadow-mode behavior; ZMQ capacity/counter
semantics; randomized playlist cycling; and successor promotion under both
control-first and media-first arrival.

## Required next validation

The immediate experimental target is the fixed-policy mode recorded in
`data-collection-mode.md`. It is intentionally separate from heuristic
validation: OAI remains PF, EdgeRIC observes in shadow mode, and the MEC uses
fixed DNN/bitrate choices and per-application FIFO execution. Deadline misses
remain measured outcomes but will not discard otherwise processable frames.
The source mode now validates fixed candidates and priority `-3`, bypasses
heuristic decisions, preserves late work through a completion-based drain, and
marks hard safety limits as invalid runs. It must still pass live preflight,
memory, worst-case drain, and completeness gates before the scenario matrix
begins. The updated Python suite passes 78 tests; Android deployment
compilation remains a target-toolchain check.

1. Build the OAI gNB, EdgeRIC, MEC, Linux UE, and Android UE with the deployment
   toolchains and immutable container inputs that will be used for experiments.
2. Validate OAI `capacity_bytes_per_prb` against the exported inputs and actual
   committed new-data grants. Compare grant-derived estimated bytes with later
   MEC-confirmed unique bytes and calibrate the payload fraction if necessary.
3. Run one UE per application, then simultaneous MEC UEs, followed by unrelated
   background UEs. Confirm automatic IP/RNTI association and that non-MEC UEs
   are never assigned MEC demand.
4. Exercise loss, reordering, reconnects, gNB/EdgeRIC restarts, RNTI reuse,
   suspension, missed configuration boundaries, model failure, and both orders
   of the prerecorded-video handoff race.
5. Measure processing, residence, transport, full frame latency, decision
   latency, result delivery, resource consumption, logging overhead, and SLO
   distributions under sustained load. Report distributions and failure
   reasons rather than only averages or successful startup.

The deployed bring-up also corrected receiver startup for an asynchronous RTP
pipeline, selected a 100-ms jitter window after a 20-ms window caused decode
loss, and raised the per-video decoded pending limit from 8 to 16 frames. Exact
image IDs, deployed configuration copies, and model hashes were not retained.

The fixed-policy campaign manifest contains 2,664 unique scenario IDs and a
locked SHA-256 for `scenarios.jsonl`. Runtime attempts use a separate
`attempt_id`; the MEC propagates the campaign/scenario/attempt join key through
MEC evidence and replay, EdgeRIC reports, and Linux and Android UE evidence.

