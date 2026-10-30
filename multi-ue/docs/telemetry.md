# Aetheredge RAN telemetry semantics

The repaired OAI source is authoritative for scheduler state. Aetheredge adds a compact ZMQ report to the existing EdgeRIC publisher; the runtime no longer reads scheduler CSV files. Scheduler trace v2 remains optional offline diagnostics.

For each UE eligible for new uplink data, OAI computes a reference transport capacity at the smallest legal configured grant. Transform precoding advances `min_grant_prb` to the next count factored only by 2, 3 and 5. OAI applies its current UL MCS, pi/2-BPSK adjustment, layers, PUSCH symbols, DMRS overhead and power limit, then calls its existing `nr_compute_tbs`. The exported value is `reference_tbs_bits / 8 / reference_prbs` in bytes per PRB.

Validation fields temporarily include source/target frame and slot, reference PRBs/TBS, MCS/table, Qm, code rate scaled by 10240, layers, start/symbol count, DMRS RE per PRB, RB overhead, TBS scaling and transform-precoding status. The latest committed grant includes PRBs, TBS, MCS, HARQ round, target and retransmission status. These fields may be removed after live validation; the derived capacity, source identity and validity remain required.

OAI exports cumulative new-data and retransmission PRB/TBS counters separately for UL and DL. EdgeRIC differences consecutive snapshots into 100-ms UL/DL grant totals, so ZMQ conflation cannot lose intermediate grants. A gNB process-generation token fences counter resets. The first snapshot after a new generation establishes a baseline and does not debit old service. The latest UL and DL grant records retain PRBs, TBS, MCS, HARQ round, frame/slot, and retransmission status.

Each latest-value radio snapshot also contains the scheduler's UL and DL MCS and BLER, PUSCH and PUCCH SNR in dB, and the frame/slot at which OAI copied the state. EdgeRIC persists the local C-RNTI with this evidence but removes it from reports sent to the MEC. Layer and direction stay explicit: aggregate DL buffer occupancy is an RLC measurement, while cumulative DL and UL SDU byte totals are labelled as MAC/RLC-interface counters. Wideband CQI, UL BSR, and opaque HARQ objects are not dataset policy features. Legacy fields may remain on the internal OAI wire for compatibility, but EdgeRIC does not relabel BSR as application backlog or scheduler throughput as unique application delivery.

Only new-data TBS provisionally reduces the oldest outstanding frame demand. Retransmission counters are retained for telemetry and do not reduce demand again. The initial `payload_fraction` is 1.0, so scheduled TBS is treated as encoded payload for this approximation. MEC reception progress remains the confirmed unique-byte measurement, is coalesced at 5 ms, and is immediate on frame completion. Live profiling must measure protocol-overhead bias and determine whether a calibrated payload fraction or HARQ-failure rollback is needed.

EdgeRIC aggregates received counter deltas into non-overlapping 100-ms reports for the MEC. Total UL opportunity remains unavailable, so those reports do not claim full-cell utilization coverage. Capacity becomes stale after the configured maximum age. Association continues using legacy `rx_bytes` only as a time-varying signature together with probe receipts, correlation and margin checks.

The 100-ms interval is an observation window, not the native RAN scheduling
period. With 30-kHz subcarrier spacing the gNB schedules 2,000 slots per second.
Persisting and transporting every slot for every UE would materially add work
to the scheduler and control path. More importantly, an encoded video frame is
fragmented across RTP packets, grants, and retransmissions, so there is no
correct one-frame/one-slot relationship. OAI therefore preserves the latest
frame/slot and UL/DL grant identity, while cumulative counters make every grant
countable even if ZMQ replaces an intermediate latest-value snapshot. EdgeRIC
differences those counters into the 100-ms windows used for analysis.

The campaign exporter joins each frame to every radio window overlapping its
UE-transmit-to-MEC-arrival interval. Historical evidence without a mapped UE
transmit wall timestamp is explicitly labelled as an arrival-instant join.
Neither case claims that the window belongs exclusively to that frame.

MEC frame-result records now include run, UE, video/task, frame, GoP, selected candidate, model, bitrate, CUDA priority, terminal outcome, and stage timestamps. UE logs retain capture/transmission and result-receipt evidence. Every 100-ms `mec_resources` event records per-application/priority host queues and in-flight submissions plus NVML device utilization and memory usage. NVML values describe the whole GPU device; they are not per-stream utilization or GPU kernel-residency measurements.

Strict slot-k to slot-k+1 targeting, late-target rejection and numerology-aligned deadline acceptance remain explicitly deferred. Current utility evaluation uses the local decision timestamp for remaining deadline time. Live validation must measure ZMQ serialization/transfer cost, scheduler cost of the reference TBS call, capacity prediction versus actual grants, report age, retransmission rate, and heuristic deadline outcomes.
