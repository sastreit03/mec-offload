# Buffered scheduler tracing

Added October 19, 2026 to repaired OAI baseline
`0769d8df7817bba5cc40916b4002cc716709aead`. No allocation, weight-lifetime,
normalization, MCS, protocol, or producer-policy changes are included.

## Enable on a future gNB launch

Set this environment variable **in the gNB process/container**:

```bash
OAI_SCHED_TRACE_FILE=/path/to/writable/scheduler.csv
```

The directory must exist and be writable. For containers, use a bind-mounted
output directory to retain the file. Use a different file for each gNB process
and preferably each capture. The variable is read during EdgeRIC initialization;
leaving it unset or empty disables tracing. There is no live toggle. This work
has not restarted or reconfigured the deployed gNB.

The file is append-only CSV. Each process launch adds a comment with PID and
monotonic/realtime clock anchors, then a column header. Times in records are
`CLOCK_MONOTONIC` nanoseconds; the startup anchors allow approximate wall-clock
correlation. Ignore `#` comment lines when reading CSV; if reusing a file, handle
repeated headers as separate sessions. Local policy sequences restart with a new
process, so do not join policies across sessions using sequence alone.

## Records and interpretation

| `kind` | Meaning |
| --- | --- |
| 1 | Accepted combined policy; gives DL/UL entry counts. Empty directions are valid. |
| 2 | One normalized weight entry; shares the accepted policy's sequence and receive time. |
| 3 | One allocation candidate outcome: committed grant or rejection. |
| 4 | Queue/drop/I/O counters, emitted approximately every second and on clean shutdown. |

| Field | Meaning |
| --- | --- |
| `time_ns` | Policy/weight record creation or allocation-decision entry time; not OTA transmission time. |
| `policy_seq` | Local monotonically increasing acceptance number. Zero means the snapshot was cleared by an empty receive, or no policy has been accepted on that scheduler thread. |
| `receive_ns` | Time immediately after successful nonblocking receive, before decoding/validation. |
| `ran_index` | Controller-echoed index, kept separately from the local sequence; it is not an absolute radio-slot sequence. |
| `direction`, `rnti` | Direction 0=DL, 1=UL; policy-summary direction is -1. RNTI is decimal. |
| `frame`, `slot` | Frame/slot of the scheduler invocation. |
| `target_frame`, `target_slot` | Grant's transmission frame/slot. UL includes k2/NTN lookahead and frame wraparound. |
| `beam` | Data-beam index when available. UL retransmission helpers receive a map rather than its beam index, so report -1. |
| `weight_present`, `weight` | 1 plus normalized value if present; 0 and -1 if absent. Explicit zero is present. |
| `reference` | Fixed first-span DL or post-retransmission UL reference budget. -1 for HARQ, where weights do not cap grants, or if not yet calculated. |
| `free_span` | Free extent examined by the scheduler, not necessarily the entire free BWP run. DL new data and changed-TDA HARQ measure their full first span; UL new data stops at the physical budget; reused-TDA HARQ stops once the required span is found. No extra map scan is added for logging. |
| `remaining` | Remaining beam PRB budget before this allocation. |
| `quota` | `floor(weight × reference)` when a weight cap applies; -1 for missing weights, exemptions, or uncalculated values. |
| `cap` | Effective quota/physical cap before UL minimum-grant, transform and power adjustments. For exempt HARQ, records the observed physical bound instead. |
| `sizing_cap` | Cap passed toward final sizing after UL grant-minimum/transform/power adjustments; may be below `cap`. |
| `stage` | Last allocation step reached. Only stage 9 means committed. |
| `exemption` | 0=none, 1=HARQ retransmission, 2=due DL timing advance, 3=UL SR/inactivity. A present weight does not constrain exempt grants. |
| `retransmission` | 1 for HARQ, 0 for new data; retransmissions retain original-TBS requirements. |
| `rb_start`, `bwp_start` | BWP-relative grant start and absolute BWP start. |
| `prbs`, `tbs`, `mcs` | Final committed PRBs, TBS in bytes and MCS. Rejections have zero PRBs/TBS; their MCS may be a preselected candidate or -1. |
| `start_symbol`, `symbols` | Final grant's symbol start/count, including the actual reused/changed retransmission TDA. |
| `dl_entries`, `ul_entries` | Expected weight-entry counts for kind 1. |
| `queued`, `dropped`, `io_errors` | Cumulative counters sampled by the writer, not at the allocation timestamp. |

`stage`: 0=candidate, 1=beam, 2=budget, 3=TDA, 4=span,
5=cap/minimum, 6=sizing/validation, 7=CCE, 8=PUCCH, 9=committed.
A rejection stage describes where evaluation stopped, not an independent error
classification. The associated budgets/caps help explain the result. -1 means
unavailable or not applicable for limit/location fields, not zero resources.

Coverage includes every candidate entering the new-data PF allocation loops,
both retransmission helpers and retransmission beam failures before helper entry.
UEs excluded before allocation (for example UL failure, no eligible demand, or an
already exhausted overall UE limit in the ranking loop) are not grant decisions
and are not logged. SR/inactivity retains the scheduler's existing combined flag;
this trace does not infer which of the two originally triggered it.

For a committed row, first check `exemption`. If it is zero, `weight_present=1`
means a weight cap was considered, while 0 means OAI physical-limit fallback.
Compare `reference`, `quota`, `cap`, `sizing_cap` and final `prbs`; a cap is a
maximum, not a promised share. Match its `policy_seq` to kind 1/2 records to inspect
the full normalized policy. Invalid policy messages retain the previous snapshot
and sequence; an empty receive clears them. Logging does not alter these rules.

## Empty policy receive frequency (trace v2)

Trace v2 appends three columns to the existing CSV. They are populated only on
`kind=4` status rows, approximately once per second and at clean shutdown;
other kinds contain zero in these columns.

| Field | Meaning |
| --- | --- |
| `policy_receive_attempts` | Completed combined DL/UL policy receives while tracing is enabled; messages + empty receives. |
| `policy_receive_messages` | Receives returning a message, including malformed/rejected messages. This is not the accepted-policy count. |
| `policy_receive_empty` | Nonblocking receives returning no message; both weight maps follow the existing clearing behavior. |

For two successive status rows, compute differences in these cumulative counters:

- Empty percentage = `100 * delta(policy_receive_empty) / delta(policy_receive_attempts)`.
- Empty receives/second = `delta(policy_receive_empty) * 1e9 / delta(time_ns)`.
- If there are no attempts in the interval, the percentage is undefined, not zero.

Counters start at zero for each gNB process. Filter status intervals to the period
after EdgeRIC has started with corrected RNTIs when evaluating steady operation;
startup/discovery empty receives are included in the full-run totals. A combined
receive is counted once, not separately for UL, DL, each UE, or each grant. The
separate MCS receive socket is excluded. An exception thrown by receive is not a
completed receive and is not counted.

The producer performs one lock-free atomic increment per completed receive when
tracing is enabled, without enqueuing a per-receive record. Thus queue drops do
not lose these counts. The background writer samples the two counters separately
and derives attempts from their sum; samples have a small boundary skew during
concurrent receives and are not an exact atomic snapshot of a radio slot. Final
shutdown totals are exact once producers stop. File failures or abrupt termination
can still lose the latest unsaved totals. No weight lifetime or scheduling behavior
is changed.

An empty receive means **no message was queued when OAI checked**. It is useful
for measuring policy availability at the receive point, but does not establish
packet loss, sender-to-gNB latency, or violation of a specified delivery deadline.
Existing v1 captures cannot provide these counters retroactively. Rebuild the gNB
build/runtime images and recreate the gNB on the next planned run to enable v2;
no EdgeRIC rebuild or Compose change is needed.

## Buffering, loss and overhead

- A pre-touched, fixed-size queue holds 8,192 records. Producers make one atomic
  gate attempt, copy a fixed record, and return. They never spin, sleep, format
  CSV, allocate heap memory, or write files. Busy/full queues increment `dropped`.
- A background `SCHED_OTHER` thread drains bounded batches approximately every
  10 ms, writes buffered CSV and flushes approximately every second. No new
  per-grant console output is added; existing OAI/EdgeRIC diagnostics are unchanged.
- The writer counts file errors separately in `io_errors`. File-open/thread-start
  failures leave scheduling running. Counters can also be read through
  `ric_trace_counters()`, including when a failed output file cannot report them.
- `queued` counts successful enqueue operations, not durable disk writes.
  `dropped` counts queue contention/overflow loss. I/O errors mean file completeness
  is unknown; they are not an exact lost-record count. Nonzero drops or missing
  policy entries mean a capture is incomplete, not that the policy never arrived.
- Clean final shutdown drains and flushes the queue. Forced termination/crashes
  can lose buffered records; there is no per-record fsync. Shutdown joins the
  writer outside scheduling and can wait for storage. There is no automatic file
  rotation; select capture duration/storage accordingly.
- Tracing is opt-in and adds timestamp, read-only weight lookup and queue-copy work
  when enabled. Disabling it skips those lookups/timestamps but leaves lightweight
  instrumentation checks. No zero-overhead or 0.5-ms deadline claim is made.
  The policy context is scheduler-thread-local, matching the existing shared
  receive followed by UL/DL scheduling on that thread. The queue supports multiple
  producers; this does not change the existing global EdgeRIC map ownership model.

## Source locations

All OAI paths below are relative to `gnb-core/ext/openairinterface5g/`:

| File | Change |
| --- | --- |
| `executables/edgeric/scheduler_trace.h` | C API, record layout, scope-exit logging macros. |
| `executables/edgeric/scheduler_trace.cpp` | Bounded queue, counters, background CSV writer and lifecycle. |
| `executables/edgeric/CMakeLists.txt` | Build the trace source and link Threads. |
| `executables/edgeric/wrapper.cpp` | Start at initialization; drain at final destruction. |
| `executables/edgeric/edgeric.cpp` | Timestamp accepted receives; log normalized maps and track clearing. |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c` | DL candidate/rejection and committed-grant records. |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c` | UL records including future target frame/slot. |

## Validation

From the parent project root:

```bash
python3 edgeric-oai-comparison/tests/test_scheduler_trace.py
python3 edgeric-oai-comparison/tests/test_scheduler_repairs.py
python3 edgeric-oai-comparison/tests/test_adapter.py
python3 edgeric-oai-comparison/tests/test_telemetry.py

docker run --rm --network none \
  -v "$PWD/gnb-core/ext/openairinterface5g:/work:ro" \
  -v "$PWD/edgeric-oai-comparison/tests:/checks:ro" \
  --entrypoint python3 ran-build-cuda:edgeric-v1 /checks/check_build.py \
  > /tmp/oai-trace-build.log 2>&1
```

Passed: production-source scheduler tests with tracing off/on and normal/debug
occupancy checks; UBSan; source comparison confirming allocation logic is unchanged
when trace statements are removed; production adapter normalized-policy records;
queue FIFO, deterministic overflow/contention loss, concurrent producers, CSV
fields, missing/zero weights, UL target wraparound, open/write failures and clean
explicit/process-exit shutdown. External resource/config/transport dependencies
remain stubbed in the focused tests.

The disposable container compiled and linked `nr-softmodem` with saved SRK/CUDA
flags and the new source, and found no unresolved libraries. Existing
`nr-uesoftmodem`/`lte-softmodem` link checks also passed. A separate fresh CMake
configure/build/link validated the EdgeRIC target and its Threads dependency;
the smoke executable was not run. This is not a full fresh OAI image rebuild.
The pre-existing `conf_file.c.o` executable-stack warning remains.

No running service/image was changed, and no commit/push was performed. Live CSV
capture, sustained-load drop rate, and enabled-tracing latency remain unmeasured.
