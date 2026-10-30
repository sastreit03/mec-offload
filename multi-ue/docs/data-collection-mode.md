# Fixed-policy data-collection mode

Decision recorded October 28, 2026. This document defines the intended
experimental behavior for collecting joined RAN, MEC, and UE features. The
source implementation is present; deployment and live multi-UE validation
remain outstanding.

## Experiment policy

- OAI uses proportional-fair (PF) radio scheduling. EdgeRIC runs in `shadow`
  mode, receives RAN telemetry, and logs features and counterfactual heuristic
  weights without publishing weights to OAI.
- The MEC uses three persistent workers, one each for detection, tracking, and
  segmentation. Each worker has one CUDA stream at priority `-3`.
- A scenario selects one fixed DNN/bitrate candidate for each application. The
  candidate remains fixed for the scenario; the MEC performs no heuristic
  admission, GoP candidate selection, suspension, or frame-priority decision.
- Each application has an independent FIFO queue. Frames from UEs using the
  same application share that queue in arrival order. Tracking additionally
  preserves frame order within each video before updating ByteTrack state.
- A five-second scenario duration is the media transmission interval. After
  transmission ends, the MEC continues draining accepted work until every
  transmitted frame has a terminal result.

## Completion and failure semantics

Deadline and SLO timestamps remain evidence for later analysis, but they must
not cause a frame to be discarded. Collection mode disables policy failures
such as `nonpositive_slack`, `decode_deadline_expired`, `deadline_expired`, and
`tracking_cutoff`. It also disables predicted-resource suspension and
admission rejection.

A transmitted frame must ultimately be recorded as completed or as a real
failure. Real failures include incomplete or corrupt transport, decoder
failure, model execution failure, invalid metadata, or an internal worker
error. Collection mode cannot convert missing radio data into a processable
frame, but lateness alone is not a failure. Results are returned even after
the nominal SLO.

Queue bounds remain resource-safety controls rather than expected loss
mechanisms. They must be sized for the bounded five-second offered load. If a
hard bound or drain watchdog is reached, the entire scenario is invalid and
the reason is recorded instead of silently treating the loss as normal data.

## Profiling requirements

Fixed collection does not require the MEC network estimator's Kalman
parameters (`P0_s2`, `R_s2`, `q_s2_per_s`, or `V_frame_s2`) or heuristic
budgets (`radio_budget`, `compute_budget_s_per_s`,
`global_queue_initial_s`, or `radio_bps_per_unit`) to make decisions. Raw
observations are still logged so estimators can be fitted or evaluated later.

ByteTrack's internal Kalman filter remains part of the tracking algorithm. It
does not require testbed calibration; it requires ordered frames and a reset
at each video boundary. Model artifacts, dependencies, worker warmup, and
successful execution remain required. Measured latency and quality profiles
remain experimental metadata but do not qualify or reject a fixed candidate.

The preliminary 90-percent-of-reference quality threshold has been removed.
The four bitrates remain 0.5, 1, 2, and 4 Mbit/s while their final DNN pairing
may be revised after profiling.

`config/profiles.measured.yaml` now contains all 48 catalog candidates. It uses
the median processing and ByteTrack times measured at CUDA priority `-3`, mean
I/P-frame sizes from the four encoding profiles, and BDD100K detection and
segmentation quality results. Each entry is marked unqualified so this
collection profile cannot enable heuristic scheduling. Tracking HOTA and the
network Kalman parameters remain unmeasured. Until ten live completions provide
a residence estimate, collection mode initializes that nonbinding estimate
from measured processing time. `tools/build_collection_profiles.py` reproduces
the file and records SHA-256 hashes for every input artifact.

## Scenario set

CUDA priority and both schedulers are fixed. The selected design contains
2,664 scenarios:

- 111 channel conditions: AWGN at three SINRs, plus TDL-C, TDL-D, CDL-C, and
  CDL-D across three SINRs, three RMS delay spreads, and three speeds;
- three cyclic application assignments;
- four common DNN/bitrate tiers; and
- separate one-UE and four-UE cases.

This is `111 × 3 × 4 × 2 = 2,664`. At five seconds of offered media per
scenario, the minimum media time is 13,320 seconds (3.7 hours), excluding
attachment, settling, draining, validation, and transitions.

The three statistical-channel speeds are 0, 1, and 10 m/s, and the
`delay_spread_ns` values are RMS delay spreads of 0, 100, and 300 ns. The SRK
channel scope is `common_bidirectional`: one SISO realization filters the
aggregate uplink and downlink waveforms, so all active UEs experience the same
filter in both directions during a scenario. A channel condition reuses its
realization seed across UE-count, application-rotation, and DNN/bitrate
ablations to support paired comparisons.

## Precomputed channel traces

The gNB exposes CIR control on TCP port 5560, separate from EdgeRIC's ZMQ
ports. Campaign runs select a precomputed trace once at the scenario boundary;
the gNB then advances through local `config.json`/`cirs.bin` data without
per-slot ZMQ messages. Each trace slot is reused for the RX and TX emulator
calls so the common SISO realization has the same time evolution in UL and DL.
The loader reports the active condition, SHA-256, playback generation, and
slot position.

`tools/build_channel_trace_bank.py` creates AWGN traces and Sionna PHY 3GPP
TDL-C, TDL-D, CDL-C, and CDL-D traces. Delays are quantized using the gNB FFT
size and subcarrier spacing returned by `config_req`; valid tap indices are
0 through 255. `tools/channel_preflight.py` checks the complete 111-condition
bank, SISO gNB configuration, Docker port mapping, radio-parameter agreement,
file hashes, binary sizes, finite coefficients, and tap-delay bounds before a
campaign starts.

Each UE stores 42 source videos with at least 1,200 frames. Eight 150-frame
segments per video provide 336 slots, of which 333 are assigned without
replacement to the 111 channel conditions and three application rotations.
The same segment is reused across all four DNN/bitrate tiers and, for Pixel 1,
the one-UE/four-UE comparison. A UE-specific deterministic permutation avoids
synchronizing identical source traffic across devices. The manifest records
the source asset hash, starting frame, and 150-frame limit.

`config/campaigns/aetheredge-fixed-policy-v1.yaml` is the authored campaign
definition. `tools/generate_campaign_manifest.py` combines it with the task
catalog and a hashed per-device media inventory to produce an immutable
`scenarios.jsonl` and `manifest.lock.json`. Scenario IDs are hashes of the
experimental factors and do not depend on randomized execution order. Runtime
attempt IDs are deliberately excluded from the manifest because retries are
preserved separately by the checkpoint database.

Before each attempt, the runner arms an idle MEC with the manifest
`campaign_id` and `scenario_id` plus a newly generated `attempt_id`. The MEC is
the runtime authority for this three-field join key. It puts the key on durable
MEC evidence and replay payloads, synchronizes it to EdgeRIC, and broadcasts it
to connected UEs. EdgeRIC radio reports and both Linux and Android UE evidence
carry the same key. Collection-mode video lifecycle commands are rejected when
the MEC is unarmed or the supplied key differs. Records created before arming
retain null scenario fields; an attempt is never inferred from timestamps.

Each assigned UE acknowledges the exact context after receiving it. The runner
requires a fresh acknowledgement from every expected alias before it trusts the
attempt. This prevents an uplink-associated UE with a failed downlink bearer
from silently omitting its video while the other UEs continue.

The arm payload also carries the complete per-UE assignment. Each UE registers
a stable alias (`pixel1` through `pixel3`, or `linux1`), resolves the exact
manifest media file, seeks to `source_start_frame`, selects the assigned
application/DNN/bitrate, and stops after 150 submitted frames. Normal camera
and shuffled prerecorded operation remain unchanged outside collection mode.
An assignment is single-use within an attempt: both the MEC and UE reject a
second video start until a new `attempt_id` supplies a new scenario context.

`tools/prepare_campaign_media.py` deterministically selects 42 videos from a
BDD100K archive or directory, verifies video metadata, optionally decodes each
file fully, calculates SHA-256 values, and writes both `SHA256SUMS` and the
per-device media inventory. The same bytes are copied to every UE; Android
resolves the manifest filename within its persisted document-tree directory,
while the Linux softUE reads the `/datasets/campaign` mount.

Campaign-wide preflight runs at initial start and after runner resumption.
A lightweight readiness and channel-application check runs before every
scenario. Post-run completeness validation runs for every scenario; the final
campaign check verifies that all 2,664 scenarios have one valid attempt.
`tools/run_campaign.py` stores attempt state in SQLite using WAL mode, marks a
running attempt interrupted after process death, and skips every scenario
already marked valid. It verifies MEC readiness, fresh EdgeRIC reports and UE
associations, selects and hashes the CIR trace, monitors terminal frame counts,
and confirms that the channel generation did not change. Transient failures
receive a new attempt ID. `scripts/campaign_recover.sh` restarts an unavailable
local EdgeRIC and, when passwordless SSH is configured, the remote MEC.
`--scenario-id` and `--limit` support pilots without creating a second manifest
or a separate checkpoint; a later full invocation skips pilot rows already valid.
An operator interrupt records the active attempt as interrupted, preserves
recovery evidence, and restarts the MEC so reconnecting UEs cannot receive the
abandoned assignment. Resuming the runner gives that scenario a new attempt ID.
A normally completed runner invocation disarms its final attempt through the
MEC control API, keeping resident workers warm and avoiding a restart.

MEC awareness messages now include a process generation UUID. EdgeRIC accepts
a reset revision counter only from a new generation, replaces associations
from its authoritative snapshot, and rejects delayed messages from retired
generations. This allows MEC restart recovery even when `run_id` is fixed.

## Implemented collection controls

`config/mec.collection.yaml` validates one fixed candidate per enabled
application and exactly one CUDA stream at priority `-3`. Collection mode
bypasses radio/compute feasibility, heuristic candidate and priority choices,
periodic reselection, and suspension. It retains receiver, model, worker, and
input-profile checks.

Decoded frames enter the existing per-application deque at fixed priority.
Deadline expiration and tracking cutoffs are not execution gates. End-of-input
waits for frames, serialized tracking, durable result preparation, and result
enqueueing. Terminal status is ordered after queued frame results. Linux and
Android retain late-frame state until those results arrive.

At end-of-input, a received access unit that cannot produce a decoded frame is
returned as an explicit per-frame decode failure; an incomplete access unit is
returned as transport loss. These are channel/media outcomes rather than a
stuck MEC drain, so they do not wait for or trigger the collection watchdog.

The initial bounded five-second configuration permits 192 pending decoded
frames and 512 MiB per video, a 192-frame tracking reorder allowance, and a
120-second drain watchdog. A hard limit or watchdog produces a failed outcome
and a `collection_run_invalid` event. These values require live peak-memory
and worst-case segmentation validation before the full campaign.

RAN reports now preserve explicit UL/DL MCS and BLER, PUSCH/PUCCH SNR,
latest and cumulative UL/DL PRB/TBS grants, frame/slot, RLC DL occupancy, and
direction-labelled MAC/RLC-interface byte totals. EdgeRIC writes local C-RNTI evidence and 100-ms grant deltas while
keeping BSR, CQI, and opaque HARQ state out of policy features. MEC results
write the selected candidate and bitrate directly, and 100-ms resource events
include per-stream host queues plus whole-device NVML utilization. Device
utilization does not measure individual CUDA-stream or kernel residency.

## Remaining work before collection

1. Keep EdgeRIC in shadow mode and verify at startup that no weight publisher
   controls OAI.
2. Add the joined dataset exporter across UE, MEC, EdgeRIC, and OAI evidence.
3. Calibrate requested emulator SINR against measured PUSCH/PUCCH SNR and BLER
   with pass-through and AWGN pilots.
4. Run a one-UE and four-UE pilot through the scenario runner, including one
   forced runner termination and resume, before starting all 2,664 scenarios.
