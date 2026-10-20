# OAI / EdgeRIC repair change list

Baseline: current SRK OAI `4353e3b2`, including independent UL weights. Locations reflect the repaired working tree. T01–T03 form a separate telemetry group.

| Item | Location | Change | Why |
| --- | --- | --- | --- |
| P01–P03, P08 | [gNB_scheduler_dlsch.c:830](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:830) | Cap DL grants by quota, free span and remaining budget; size before control reservation. | Prevent oversized grants and reject unusable allocations safely. |
| P02 | [gNB_scheduler_primitives.c:712](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c:712) | Replace the custom weighted search with a bounded wrapper around OAI sizing. | Fix incorrect RB counts and partial-grant results. |
| P03, P09 | [gNB_scheduler_primitives.c:626](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c:626) | Reject invalid ranges, zero outputs, and bound transform-precoding rounding. | Prevent zero-input loops and invalid PRB counts. |
| P04, P08 | [gNB_scheduler_dlsch.c:444](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:444) | Use a local retransmission candidate and enforce remaining budget. | Preserve HARQ history when allocation fails. |
| P04–P06 | [scheduler_allocation.h:52](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/scheduler_allocation.h:52) | Mark exactly the allocated count at BWP start + relative grant start. | Fix occupancy marking in all four DL/UL grant paths. |
| P07 | [gNB_scheduler_ulsch.c:1811](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c:1811) | Search complete free spans for reused-TDA retransmissions. | Avoid collisions with occupied PRBs inside a candidate span. |
| P08–P09 | [gNB_scheduler_ulsch.c:2179](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c:2179) | Bound UL grants for every policy state; validate transform counts after power adjustment. | Keep grants within resources, including missing-policy and SR/inactivity paths. |
| P10 | [gNB_scheduler_primitives.c:882](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c:882); [wrapper.cpp:205](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/edgeric/wrapper.cpp:205) | Keep MCS proposals as integers; use INT_MAX for missing/null and check effective limits. | Avoid sentinel truncation and out-of-range overrides. |
| P10 | [edgeric.cpp:485](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/edgeric/edgeric.cpp:485) | Validate wire RNTI/MCS values before conversion; guard incomplete pairs. | Prevent invalid values such as 256 becoming valid-looking MCS zero. |
| P11 | [scheduler_allocation.h:36](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/scheduler_allocation.h:36) | Add precommit grant checks and optional debug occupancy scans. | Catch invalid grants without adding normal-path scans or logging. |
| Contract | [mac_proto.h:437](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/mac_proto.h:437) | Document invalid-range versus usable partial-grant outputs. | Clarify when a false sizing result can still be scheduled. |
| T01 | [gNB_scheduler_dlsch.c:366](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:366) | Report one completed DL buffer aggregate per UE, including zero. | Avoid overwritten totals and stale drained-buffer reports. |
| T02 | [gNB_scheduler_dlsch.c:664](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:664) | Use floating-point division for UL PUSCH SNR. | Preserve fractional and negative dB values. |
| T03 | [gNB_scheduler_dlsch.c:727](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:727) | Remove the tiny TBS multiplier; identify the hypothetical PF estimate. | Report the estimate accurately without implying actual delivered data. |

| Supporting file | Change / purpose |
| --- | --- |
| [tests/test_scheduler_repairs.py](tests/test_scheduler_repairs.py), [scheduler_cases.c](tests/scheduler_cases.c), [retransmission_stubs.h](tests/retransmission_stubs.h) | Production-source allocation, sizing, MCS, retransmission and cleanup tests. |
| [tests/test_adapter.py](tests/test_adapter.py) | Check policy fallback, normalization and MCS input handling. |
| [tests/test_telemetry.py](tests/test_telemetry.py) | Check buffer aggregation, SNR and PF estimate reporting. |
| [tests/check_build.py](tests/check_build.py) | Reproduce isolated incremental compile/link checks. |
| [tests/README.md](tests/README.md) | Test commands and coverage limits. |
| [repairs/README.md](repairs/README.md) and three patch files | Separate allocation, defensive and telemetry review groups. |
| [EDGERIC-BUILD-INTEGRATION.md](EDGERIC-BUILD-INTEGRATION.md#12-bounded-scheduler-repairs-october-16-2026) | Detailed findings, build results and remaining live checks. |

Policy normalization, receive/clear cadence, PF order, partial grants and the hardcoded producer were preserved. Due DL TA and UL SR/inactivity retain weight exemptions within physical limits.

Focused tests and incremental compile/link checks passed. The existing executable-stack warning remains; live radio, power-limited scheduling and deadline validation are still required. No deployment, restart, commit or push was performed.


### Empty policy receive measurement

- `executables/edgeric/edgeric.cpp`: count each completed combined-policy receive;
  distinguish no message from a received message before validation.
- `executables/edgeric/scheduler_trace.{h,cpp}`: lock-free counters, exported in
  one-second CSV status rows; independent of queue drops, no behavior changes.
- `tests/test_scheduler_trace.py`, `tests/test_adapter.py` (comparison folder):
  verify counts, invalid messages, disabled tracing, overflow and concurrency.
- `SCHEDULER-TRACE.md`, `EDGERIC-BUILD-INTEGRATION.md`: document formulas and rebuild
  requirements. Source paths above are relative to the OAI submodule.
