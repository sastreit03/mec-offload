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
  CDL-D across three SINRs, three delays, and three speeds;
- three cyclic application assignments;
- four common DNN/bitrate tiers; and
- separate one-UE and four-UE cases.

This is `111 × 3 × 4 × 2 = 2,664`. At five seconds of offered media per
scenario, the minimum media time is 13,320 seconds (3.7 hours), excluding
attachment, settling, draining, validation, and transitions.

The three statistical-channel speeds are 0, 1, and 10 m/s. The SRK channel
scope is `common_uplink`: one realization filters the aggregate uplink signal,
so all active UEs experience the same filter in a scenario. A channel
condition reuses its realization seed across UE-count, application-rotation,
and DNN/bitrate ablations to support paired comparisons.

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
   controls OAI. Add joinable scenario and wall-clock evidence where current
   EdgeRIC events expose only a host-local monotonic clock.
2. Generate a scenario manifest containing the scenario ID, UE/application
   assignment, candidate and bitrate, channel parameters, PF and shadow modes,
   CUDA priority, duration, seeds, and code/config/model/container identities.
3. Add a preflight check and a post-run completeness check. A valid run has one
   terminal result for every transmitted frame and no queue-bound, worker,
   transport, decoder, or logging failure.

The scenario generator and joined dataset exporter will be specified after the
remaining experiment-control decisions are supplied.
