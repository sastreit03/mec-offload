# Scheduler repair review groups

These sequential OAI-only patch exports are **already applied** to the working tree.
Do not apply them there again. They are review/reproduction artifacts against OAI
`4353e3b2fc3b36884f336e606c79d3ecc6267328` on `srk-edgeric`.

1. `01-allocation.patch`: P01–P08 and P11 allocation/occupancy/cleanup changes.
2. `02-defensive.patch`: P03/P09 shared sizing guards and transform bounds,
   P10 MCS input/getter validation, and shared-helper contract documentation.
3. `03-telemetry.patch`: T01–T03 only, separately reviewable/revertible.

Apply the complete series in order to that clean baseline if reproducing in a
separate checkout. Intermediate groups were not independently validated. The
series was applied to temporary baseline files and compared byte-for-byte with
the final working sources. Tests live in `../tests/`; results, exact commands,
policy exceptions and remaining live checks are in `../EDGERIC-BUILD-INTEGRATION.md`,
section 12. No commits were created.


`04-scheduler-trace.patch` is a separate October 19 opt-in logging change against
repaired baseline `0769d8df7817bba5cc40916b4002cc716709aead`. It is already applied
to the current source and is not part of `00-combined-repairs.patch`. Its seven
source files were reproduced from that baseline and verified byte-for-byte.
Usage and validation are in `../SCHEDULER-TRACE.md`; test harness changes are in
the parent repository's `../tests/` directory.
