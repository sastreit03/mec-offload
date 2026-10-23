# Aetheredge RAN telemetry semantics

The repaired OAI source is authoritative for scheduler state. Aetheredge adds a compact ZMQ report to the existing EdgeRIC publisher; the runtime no longer reads scheduler CSV files. Scheduler trace v2 remains optional offline diagnostics.

For each UE eligible for new uplink data, OAI computes a reference transport capacity at the smallest legal configured grant. Transform precoding advances `min_grant_prb` to the next count factored only by 2, 3 and 5. OAI applies its current UL MCS, pi/2-BPSK adjustment, layers, PUSCH symbols, DMRS overhead and power limit, then calls its existing `nr_compute_tbs`. The exported value is `reference_tbs_bits / 8 / reference_prbs` in bytes per PRB.

Validation fields temporarily include source/target frame and slot, reference PRBs/TBS, MCS/table, Qm, code rate scaled by 10240, layers, start/symbol count, DMRS RE per PRB, RB overhead, TBS scaling and transform-precoding status. The latest committed grant includes PRBs, TBS, MCS, HARQ round, target and retransmission status. These fields may be removed after live validation; the derived capacity, source identity and validity remain required.

OAI exports cumulative new-data and retransmission PRB/TBS counters. EdgeRIC differences consecutive snapshots, so ZMQ conflation cannot lose intermediate grants. A gNB process-generation token fences counter resets. The first snapshot after a new generation establishes a baseline and does not debit old service.

Only new-data TBS provisionally reduces the oldest outstanding frame demand. Retransmission counters are retained for telemetry and do not reduce demand again. The initial `payload_fraction` is 1.0, so scheduled TBS is treated as encoded payload for this approximation. MEC reception progress remains the confirmed unique-byte measurement, is coalesced at 5 ms, and is immediate on frame completion. Live profiling must measure protocol-overhead bias and determine whether a calibrated payload fraction or HARQ-failure rollback is needed.

EdgeRIC aggregates received counter deltas into non-overlapping 100-ms reports for the MEC. Total UL opportunity remains unavailable, so those reports do not claim full-cell utilization coverage. Capacity becomes stale after the configured maximum age. Association continues using legacy `rx_bytes` only as a time-varying signature together with probe receipts, correlation and margin checks.

Strict slot-k to slot-k+1 targeting, late-target rejection and numerology-aligned deadline acceptance remain explicitly deferred. Current utility evaluation uses the local decision timestamp for remaining deadline time. Live validation must measure ZMQ serialization/transfer cost, scheduler cost of the reference TBS call, capacity prediction versus actual grants, report age, retransmission rate, and heuristic deadline outcomes.
