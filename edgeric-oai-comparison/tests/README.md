# Focused OAI repair checks

Run from the parent project root:

```bash
python3 edgeric-oai-comparison/tests/test_scheduler_repairs.py
python3 edgeric-oai-comparison/tests/test_adapter.py
python3 edgeric-oai-comparison/tests/test_telemetry.py
```

Requires host Python 3, `cc`, and `c++` with UBSan. Tests extract current production
functions/blocks every run; fixtures replace external dependencies, not the
allocation search or TBS calculation. Generated files/executables live in temporary
directories. Scheduler tests run both with and without occupancy debug scans.
Adapter diagnostics for invalid inputs are expected.

`check_build.py` is intended only for a disposable `ran-build-cuda:edgeric-v1`
container, with the current OAI tree mounted read-only at `/work`. It copies the
current scheduler/header/main/adapter/generated sources into the container, executes
the saved Ninja compiler/archive/link commands, checks gNB shared-library resolution,
and attempts the other saved softmodem link commands. See section 12 of the
integration notes for the exact `docker run` command and incremental-build limits.
It does not run a modem, deploy an image, or access the network.

Limitations: CCE/PUCCH/FAPI/configuration/transport are boundary stubs. Tests of
post-power rounding supply simulated power-adjusted counts. The full PF loops,
actual RF power model, SIB procedure, beam allocator, wire protocol and live radio
are not tested end to end. Source-order guards supplement runtime tests to check
validation precedes resource commitment. This is not a complete scheduler audit.


Scheduler tracing additions (October 19): `test_scheduler_trace.py` tests the
production bounded writer, drop counters, file errors and shutdown, and verifies
allocation source is unchanged after removing trace-only statements. Scheduler
regressions now run with logging off/on; adapter tests inspect accepted-policy
CSV records. `check_build.py` additionally compiles the new trace translation unit
using saved wrapper flags and performs a fresh isolated EdgeRIC CMake link check.
See `../SCHEDULER-TRACE.md` for usage and limitations.
