Comparison performed on 2026-09-22. Both code trees were inspected read-only. Only analysis artifacts were written, under `/tmp/edgeric-oai-comparison/`. No builds, deployments, checkouts, patch application, or code edits were performed.

**Main finding.** EdgeRIC is a small MAC-scheduler integration added to an OAI snapshot closely matching `2024.w34`. Your installed Sionna RK tree uses `2025.w34` plus 36 patched paths. Most differences between these directories therefore come from a year of upstream OAI development. EdgeRIC’s separate `oai-cn5g` deployment/configuration files are unchanged copies of the original OAI tutorial core.

**Repositories and baseline evidence**

| Item | Inspected version |
|---|---|
| EdgeRIC repository | `aa07918b45c24a7713a4048287fa5435e798fa69` |
| Closest upstream OAI baseline for EdgeRIC | `2024.w34`, commit `46a1d2a621ca798df0c1a3aea553fac7013a37d8`, 2024-08-28 |
| Your Sionna OAI HEAD | `2025.w34`, commit `207aac94d13e714abac8062ebf869805f2acd6dc`, 2025-08-22 |
| Sionna local modifications | 36 staged paths; all 36 resulting object hashes match `gnb-core/patches/openairinterface5g.patch`; no unstaged tracked-file changes |

EdgeRIC vendors the OAI files into its parent repository, without retaining OAI history as a nested repository. The baseline was inferred by comparing content hashes against locally available OAI weekly tags from 2024 and nearby mainline commits. `2024.w34` is the closest match: 3,759 entries have identical content; eight existing files differ; 15 files are added; four tracked paths are missing. This is strong content evidence, not proof of the original author's checkout command. EdgeRIC’s initial import was November 26, 2024; later relevant commits change `edgeric.cpp` on December 1, 2024 and add an Amarisoft configuration on May 5, 2025.

**Direct comparison of the requested directories**

| Tracked-path comparison | Count |
|---|---:|
| Same file contents | 2,273 |
| Same path, different contents | 1,144 |
| Present only in EdgeRIC | 365 |
| Present only in your Sionna tree | 533 |

The comparison uses actual working-file contents, including Sionna's staged patches. Nested FlexRIC is represented by its submodule commit, rather than expanding its internal files; it is absent from the EdgeRIC snapshot. There are no additional nonignored untracked files in the Sionna OAI directory. Its ignored `host_product_family` is generated host metadata and excluded. Git metadata and build products are excluded. Counts compare paths without rename matching, so an upstream rename appears as one path on each side.

Executable permissions differ on 3,346 shared paths, including 2,221 whose contents are identical. Against its own 2024 baseline, EdgeRIC changes executable permissions on 3,685 shared paths. These broad permission changes should not be confused with source changes.

The upstream transition alone, `2024.w34` → `2025.w34`, has a Git-reported 1,918 changed files, 148,579 insertions and 125,838 deletions. This includes PHY, MAC/RRC, NAS, NFAPI, radio drivers, build/CI and documentation. The newer MAC scheduler has changed interfaces and frame/beam handling. Copying complete 2024 scheduler files over the 2025 Sionna versions would discard upstream changes.

**The eight existing files EdgeRIC changes relative to 2024.w34**

| File relative to `openairinterface5g/` | Change |
|---|---|
| `CMakeLists.txt` | Requires C++17, adds `executables/edgeric`, and links the `edgericc` library into `nr-softmodem`. |
| `executables/nr-softmodem.c` | Adds global EdgeRIC agent and TTI counter; creates/initializes the agent at program startup and destroys it during shutdown. |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c` | Collects per-UE metrics, publishes reports, receives MCS and scheduling weights, and calls new MCS/RB allocation helpers. |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c` | Adds `nr_find_nb_rb_new()` for controller-weighted RB caps and `get_mcs_from_bler_new()` for externally supplied downlink MCS. |
| `openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c` | Adds uplink smoothed-throughput and scheduled-byte reporting. Does not replace uplink scheduling with the new weighted allocator/MCS helper. |
| `openair2/LAYER2/NR_MAC_gNB/mac_proto.h` | Declares the two new helpers. |
| `targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.106PRB.usrpb210.conf` | Adds `min_rxtxtime=5` and AWGN channel-model configurations, with `noise_power_dB=-50` for gNB and UE. |
| `targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.162PRB.2x2.usrpn300.conf` | Changes USRP data address from `192.168.10.2` to `192.168.11.2`, retaining management address `192.168.10.2`. |

**New files and omitted paths**

Eleven files implement the integration under `executables/edgeric/`: `CMakeLists.txt`, `edgeric.cpp`, `edgeric.h`, `wrapper.cpp`, `wrapper.h`, and generated `.pb.cc`/`.pb.h` pairs for `metrics`, `control_weights`, and `control_mcs`. The C wrapper makes the C++ client callable from OAI's C code. Dependencies are ZeroMQ and Protobuf; the repository README requests Protobuf 3.21.12. The `.proto` sources live outside the OAI subtree in `EdgeRIC-v2/protobufs/`.

Three other additions are exact copies of upstream files: `CMakeLists1212.txt`, `openair2/LAYER2/NR_MAC_gNB/backup/gNB_scheduler_dlsch.c`, and `backup/gNB_scheduler_ulsch.c`. They are backups, not independent active implementations.

The final addition is `gnb.sa.band78.fr1.162PRB.2x2.usrpn300_amari.conf`. Despite its filename, it configures 106 PRBs. Relative to the accompanying N300 config, it changes SSB/Point-A frequencies, BWP settings, PRACH settings, PUCCH nominal power, and the TDD pattern to a 2.5 ms period with two DL slots, two UL slots, and six DL/four UL symbols in the mixed slot.

Missing upstream paths are `doc/testing_gnb_w_cots_ue_resources/oai_enb.log`, `oai_gnb.log`, `openair3/NAS/UE/API/USER/tst/at_parser.out`, and the `openair2/E2AP/flexric` gitlink. The `.gitmodules` declaration for FlexRIC remains in EdgeRIC, but its actual submodule entry is missing. The EdgeRIC ZeroMQ connection is separate from OAI's standard FlexRIC E2 agent.

**Actual controller behavior**

The client binds metrics publication to `tcp://127.0.0.1:5555`, connects to scheduling weights on port 5556, and connects to MCS commands on port 5557. These endpoints are hard-coded. Receives are nonblocking, and both subscriber sockets use conflation to retain the latest message. The integration is initialized unconditionally in this gNB executable.

Reports contain the TTI counter, RNTI, CQI, SNR, TX/RX values, DL/UL buffer fields and a DL TBS estimate. Reporting and the TTI-counter increment happen in the downlink proportional-fair scheduling function; the counter is not directly the absolute NR frame/slot number. The TX/RX fields are populated from exponentially smoothed per-scheduling-call byte values. The DL buffer setter runs per logical channel and overwrites the per-RNTI entry; it is not a sum across all logical channels. The UL buffer field comes from `sched_ul_bytes`, not the UE's reported outstanding backlog. DL TBS is the scheduler's one-RB estimate used for ranking, not the final granted transport block size.

The existing proportional-fair UE sorting and HARQ handling remain. External weights, normalized on reception, change the per-UE RB cap as `weight * rb_total_num`. Thus the external weights affect allocation size, rather than replacing the PF ordering coefficient. The total is the first eligible allocation's contiguous free-RB span. External MCS overrides the BLER helper at its update point; the early return preserves the previous MCS within the ten-frame BLER update window. The HARQ-disabled path still directly uses `max_mcs`.

When no new message is available, the respective received-weight/MCS map is cleared. The control values are not simply held indefinitely. Missing weights leave the incoming RB cap unchanged; missing MCS lets the BLER logic operate, subject to its normal update timing.

**Consequential implementation differences found by inspection**

- `gNB_scheduler_primitives.c:765` comments out `*nb_rb = hi` in the new binary-search allocator. When the requested byte count lies between the minimum and maximum TBS, the function keeps `nb_rb_min` instead of returning the searched RB count. The DL caller ignores its Boolean result. This can change allocations even without controller weights.
- The same new helper returns `true` when `bytes > *tbs` at maximum allocation; the original helper returns `false`. Its controller-weighted maximum also replaces the current contiguous-space maximum without clamping it back to that maximum.
- The MCS helper directly installs the controller value without reapplying `max_mcs` after the override. Incoming control arrays are read in RNTI/value pairs without an explicit even-length check.

These are static observations of the source, not results from radio/runtime testing.

**What your Sionna patches add instead**

Your 36 local patched paths exactly match the resulting hashes recorded in `gnb-core/patches/openairinterface5g.patch`. They add plugin build/initialization hooks, receiver and neural-demapper callbacks, RX/TX channel-emulation hooks, worker-thread lifecycle hooks, modular LDPC/thread initialization changes, host/DGX build selection, five Dockerfiles including CUDA gNB/UE build images and FlexRIC, and FlexRIC MAC reporting adjustments. The local MAC reporting patch removes DL/UL slot checks around current-byte/current-RB reporting. Another behavioral change replaces a fatal full-PRACH-list assertion with reuse of the entry having the smallest frame/slot values.

The Sionna patch changes the FlexRIC revision from upstream `df754a85537f6db255b5330b8d83eda6a2742f25` to `1f571c18b81deeb627ffce1102fea3f9da3f62a7`; that nested checkout is clean. It does not add EdgeRIC's client or patch the four EdgeRIC scheduler/header files. At the patch level, the two integrations both edit `CMakeLists.txt` and `executables/nr-softmodem.c`; porting also requires adapting the old EdgeRIC scheduler additions to the newer upstream scheduler APIs. Sionna's plugin implementation directories outside the requested OAI subtree are not expanded in this inventory.

**EdgeRIC oai-cn5g versus original OAI**

The appropriate reproducible original is OAI `2024.w34:doc/tutorial_resources/oai-cn5g/`, whose six files match the EdgeRIC deployment byte-for-byte. This was verified against the upstream Git objects in your OAI clone, not merely against EdgeRIC's embedded duplicate.

| EdgeRIC core file | Result against original OAI |
|---|---|
| `docker-compose.yaml` | Identical |
| `conf/config.yaml` | Identical |
| `conf/sip.conf` | Identical |
| `conf/users.conf` | Identical |
| `database/oai_db.sql` | Identical |
| `healthscripts/mysql-healthcheck.sh` | Identical |
| `data/queries.active` | Extra 20,001-byte file, mostly zero-filled; not referenced by this Compose deployment |

Consequently, the IMS container/configuration, PLMN 001/01, DNNs `oai`/`openairinterface`/`ims`, subscriber database, UPF NAT configuration, and bridge subnet `192.168.70.128/26` are inherited from the original tutorial. They are not EdgeRIC customizations.

This directory contains deployment files, not AMF/SMF/UPF or other NF source trees. Compose refers to official `oaisoftwarealliance/oai-*:develop` images, `ims:latest`, `trf-gen-cn5g:jammy`, and `mysql:8.0`. There is no custom NF build or source patch here. Floating image tags do not identify an immutable NF source revision, so this inspection establishes unchanged checked-in configuration and the absence of bundled NF source modifications; it does not establish which image binary was run on another machine.

**Evidence files**

- [Complete content inventory](ran-all-files.tsv), including mode differences.
- [Only differing content/presence](ran-content-differences.tsv).
- [Complete text comparison: your Sionna tree → EdgeRIC](sionna-to-edgeric.diff), approximately 20 MB. Binary differences and the FlexRIC gitlink are described, not encoded as applyable patches; executable modes are in the TSV inventory.
- [Focused diff of EdgeRIC's eight modified existing files](edgeric-modified-files.diff), upstream 2024.w34 → EdgeRIC.
- [All 27 EdgeRIC changes relative to the baseline](edgeric-vs-upstream-files.tsv).
- [Your Sionna patches versus upstream 2025.w34](sionna-local-changes.diff).
- [Upstream version-change file statistics](upstream-2024w34-to-2025w34.stat).
- [Core file hashes proving equality](core-upstream-verification.tsv).
- [Comparison script](compare.py), which only reads the repositories and writes analysis artifacts in this directory.
