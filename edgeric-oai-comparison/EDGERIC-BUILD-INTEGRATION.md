# EdgeRIC integration into SRK OAI

This records the build integration, main-program initialization, and downlink
scheduler port from EdgeRIC's OAI 2024.w34-based tree into SRK's OAI
2025.w34-based tree.

The user reported successful base, build, and gNB image builds after the
CMake, dependency, C++ API, and TensorRT changes. Those successful builds
preceded the subsequent `nr-softmodem.c` and downlink scheduler edits.
The two scheduler helper definitions and their declarations are now present.
The uplink telemetry additions are now present; executable-linkage checks
and full build/runtime validation remain outstanding.
The later changes have not been build- or runtime-validated.

All file paths are relative to `~/mec-offload-ad/` unless linked absolutely.
Line numbers below refer to the working-tree files at the time of this merge;
they will shift with later edits. Each link opens the beginning of the block.

## Preparation

- Copied the EdgeRIC files marked `added` in
  `edgeric-oai-comparison/edgeric-vs-upstream-files.tsv` into the SRK OAI tree,
  excluding the two scheduler files under EdgeRIC's `backup/` directory.
- Left files marked `removed` in place.
- Backed up the SRK versions of files marked `modified` under
  `srk-file-backups/gnb-core/ext/openairinterface5g/` before integrating changes.
- Registered `gnb-core/ext/openairinterface5g/` as a submodule backed by
  `git@github.com:sastreit03/openairinterface5g-srk.git`, branch `srk-edgeric`.
  OAI changes belong in that repository; the neural receiver change below
  belongs in the parent `mec-offload-ad` repository.

## 1. OAI root CMakeLists.txt

File: `gnb-core/ext/openairinterface5g/CMakeLists.txt`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Add EdgeRIC, require C++17, link gNB | [lines 1863–1871](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/CMakeLists.txt:1863) |


Immediately after this line:

```cmake
target_link_libraries(nr-softmodem PRIVATE asn1_nr_rrc_hdrs asn1_lte_rrc_hdrs)
```

Added the following beside the existing `nr-softmodem` link declarations:

```cmake
# Link edgeric
add_subdirectory(executables/edgeric)

set_target_properties(edgericc PROPERTIES
  CXX_STANDARD 17
  CXX_STANDARD_REQUIRED ON
)

target_link_libraries(nr-softmodem PRIVATE edgericc)
```

This adds the copied EdgeRIC library to the existing SRK build and links it
to the gNB executable. C++17 supports the source's `[[maybe_unused]]`
annotation; SRK already uses C++17, and the target property makes the
requirement explicit. The SRK root CMake file was retained rather than
replaced with EdgeRIC's older version.

The copied `executables/edgeric/CMakeLists.txt` already calls
`find_package(Protobuf REQUIRED)`, builds `edgericc` from the EdgeRIC wrapper
and generated protobuf sources, and links protobuf and ZeroMQ. It did not
require an additional edit in this step.

## 2. EdgeRIC C++ source

File: `gnb-core/ext/openairinterface5g/executables/edgeric/edgeric.cpp`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Unused-helper annotation | [line 19](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/edgeric/edgeric.cpp:19) |
| Weight subscriber options | [lines 77–80](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/edgeric/edgeric.cpp:77) |
| MCS subscriber options | [lines 86–91](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/edgeric/edgeric.cpp:86) |


Replaced the active legacy socket-option calls with cppzmq's typed API:

```cpp
subscriber_weights.set(zmq::sockopt::subscribe, "");
subscriber_weights.set(zmq::sockopt::conflate, true);

subscriber_mcs.set(zmq::sockopt::subscribe, "");
subscriber_mcs.set(zmq::sockopt::conflate, true);
```

Both subscribers still subscribe to all topics and enable conflation. These
calls replace the legacy `zmq_setsockopt`/`setsockopt` usage, including the
deprecated C++ overloads that can fail compilation with warnings treated as
errors. The shared `int conflate = 1` variable is no longer needed. The
current file retains the old calls as comments.

Also annotated the currently unused helper:

```cpp
[[maybe_unused]] static int s_send(void *socket, char *string)
```

This avoids an unused-function warning becoming a build error. Socket
endpoints and the message-processing logic were retained.

## 3. CUDA base Dockerfile: development dependencies

File: `gnb-core/ext/openairinterface5g/docker/Dockerfile.base.ubuntu.cuda`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Add development/checking packages | [lines 61–64](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/docker/Dockerfile.base.ubuntu.cuda:61) |


Added these packages to the existing apt installation:

| Package | Purpose |
| --- | --- |
| `cppzmq-dev` | C++ ZeroMQ header, including `zmq.hpp` and typed socket options |
| `libprotobuf-dev` | C++ protobuf headers and link-time development files |
| `protobuf-compiler` | `protoc`, available if message code needs regeneration |
| `pkg-config` | Dependency presence and version checks |

`libzmq3-dev` was already installed and was retained. The copied protobuf
`.pb.cc`/`.pb.h` sources were used; no regeneration was performed in this step.

## 4. CUDA build Dockerfile: dependency checks

File: `gnb-core/ext/openairinterface5g/docker/Dockerfile.build.ubuntu.cuda`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Check development dependencies and protobuf version | [lines 44–47](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/docker/Dockerfile.build.ubuntu.cuda:44) |


Added this check after copying OAI and the SRK plugins, before compilation:

```dockerfile
RUN pkg-config --exists protobuf libzmq && \
    test -r /usr/include/zmq.hpp && \
    pkg-config --modversion protobuf libzmq && \
    test "$(pkg-config --modversion protobuf)" = "3.21.12"
```

This fails early for missing dependencies and explicitly requires protobuf
3.21.12 for the current copied generated sources. It checks the installed
version; it does not pin the apt package. A future protobuf upgrade requires
revisiting this check and generated-code compatibility.

The build context must be `gnb-core/`, because this Dockerfile copies both
`ext/openairinterface5g/` and `plugins/`.

## 5. CUDA gNB Dockerfile: runtime dependency

File: `gnb-core/ext/openairinterface5g/docker/Dockerfile.gNB.ubuntu.cuda`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Add protobuf runtime | [line 77](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/docker/Dockerfile.gNB.ubuntu.cuda:77) |
| Fail on unresolved runtime libraries | [lines 156–163](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/docker/Dockerfile.gNB.ubuntu.cuda:156) |


Added `libprotobuf32t64` to the runtime image's apt installation for the
current Ubuntu 24.04 image. Retained its existing ZeroMQ package.

Added a check after installing/copying runtime libraries:

```dockerfile
RUN set -eu; \
    ldd /opt/oai-gnb/bin/nr-softmodem > /tmp/gnb-dependencies.txt; \
    cat /tmp/gnb-dependencies.txt; \
    if grep -q 'not found' /tmp/gnb-dependencies.txt; then \
        echo "ERROR: nr-softmodem has unresolved runtime libraries" >&2; \
        exit 1; \
    fi; \
    rm /tmp/gnb-dependencies.txt
```

The explicit `not found` check makes unresolved executable dependencies fail
the image build. It does not validate inference or every dynamically loaded
plugin's behavior.

## 6. SRK neural receiver: TensorRT build compatibility

File: `gnb-core/plugins/neural_receiver/src/runtime/trt_receiver.cpp`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Version-dependent context creation | [lines 435–440](/home/sstreit/mec-offload-ad/gnb-core/plugins/neural_receiver/src/runtime/trt_receiver.cpp:435) |
| Null-context check | [lines 442–445](/home/sstreit/mec-offload-ad/gnb-core/plugins/neural_receiver/src/runtime/trt_receiver.cpp:442) |
| Older TensorRT workspace allocation | [lines 447–452](/home/sstreit/mec-offload-ad/gnb-core/plugins/neural_receiver/src/runtime/trt_receiver.cpp:447) |


The initial build passed the EdgeRIC compilation but failed in the SRK neural
receiver because the installed TensorRT headers no longer exposed
`createExecutionContextWithoutDeviceMemory()` and `getDeviceMemorySize()`.
Updated context initialization to use the same version-dependent approach
already present in the neural demapper:

```cpp
#if NV_TENSORRT_MAJOR >= 10
    context.trt = engine->createExecutionContext(
        nvinfer1::ExecutionContextAllocationStrategy::kSTATIC);
#else
    context.trt = engine->createExecutionContextWithoutDeviceMemory();
#endif

    if (context.trt == nullptr) {
        std::fprintf(stderr, "Failed to create TensorRT receiver context\n");
        std::abort();
    }

#if NV_TENSORRT_MAJOR < 10
    size_t preallocSize = engine->getDeviceMemorySize();
    void* preallocMem = nullptr;
    CHECK_CUDA(cudaMalloc(&preallocMem, preallocSize));
    context.trt->setDeviceMemory(preallocMem);
#endif
```

TensorRT 10 and newer manage the context workspace with `kSTATIC`. Older
versions retain manual allocation, now with a checked CUDA allocation. The
null-context check applies to both branches. This change addresses the
compiler error; the older-version branch and inference behavior have not
been independently tested here.

## 7. gNB main program: EdgeRIC lifecycle

File: `gnb-core/ext/openairinterface5g/executables/nr-softmodem.c`

Current change locations:

| Change | Working-tree location |
| --- | --- |
| C wrapper include | [line 39](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/nr-softmodem.c:39) |
| Shared agent and counter | [lines 94–95](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/nr-softmodem.c:94) |
| Create and initialize agent | [lines 554–555](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/nr-softmodem.c:554) |
| Destroy and clear agent | [lines 751–752](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/executables/nr-softmodem.c:751) |

Added the C wrapper include outside `#ifdef E2_AGENT`. EdgeRIC's wrapper
is separate from the existing FlexRIC E2 agent:

```c
#include "edgeric/wrapper.h"
```

Defined the shared globals once in this executable, matching the wrapper's
`extern` declarations. They remain non-static so the scheduler can use them:

```c
EdgeRIC *agent = NULL;
int tti_counter = 0;
```

Added initialization immediately after `softmodem_verify_mode(...)`:

```c
agent = ric_create();
ric_init(agent);
```

EdgeRIC originally initializes at the very beginning of `main()`. The port
initializes after configuration/mode validation and after the existing
`start_background_system()` call, which forks, but before gNB tasks start.
The current wrapper binds the publisher to `127.0.0.1:5555` and connects
subscribers to ports 5556 and 5557.

Added final cleanup after `time_manager_finish()` and before `free(pckg)`:

```c
ric_destroy(agent);
agent = NULL;
```

Cleanup remains in final shutdown, not in `stop_L1()`, which also participates
in temporary stop/restart behavior. This does not add cleanup to early-exit
paths. Preserve 2025.w34's startup/shutdown flow, time manager, and the
existing SRK plugin include and `init_plugins(fp)` call. These lifecycle
changes do not themselves report metrics or apply scheduling policies.

## 8. Downlink scheduler: EdgeRIC port

### Scope and references

Ported EdgeRIC's additions to
`gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c`
while retaining the surrounding OAI 2025.w34 scheduler. This file had no
additional SRK differences from 2025.w34 before this edit.

Implementation reference:
`~/EdgeRIC-5G-OAI/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/`.

Paper: [EdgeRIC: Empowering Realtime Intelligent Optimization and Control in NextG Networks](/home/sstreit/Downloads/edgeric.pdf),
arXiv:2304.11199v3, particularly sections 4.4, 5.1, and 6.1. The paper
describes weight-based resource allocation and an srsRAN implementation;
the supplied OAI repository is the reference for this OAI port's exact
behavior. The paper does not establish that every detail of that later
OAI implementation matches its scheduler.

### Changes made in gNB_scheduler_dlsch.c

Current change locations:

| Change | Working-tree location |
| --- | --- |
| Wrapper include | [line 45](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:45) |
| Per-channel buffer report | [line 351](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:351) |
| Poll MCS commands | [line 629](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:629) |
| Throughput/SNR/CQI reports | [lines 649–651](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:649) |
| External MCS helper call | [line 698](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:698) |
| PF TBS report | [line 712](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:712) |
| Counter, metrics, per-beam budgets, weight polling | [lines 734–744](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:734) |
| Capture first span per beam | [lines 813–816](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:813) |
| Weighted RB helper call | [lines 880–892](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c:880) |


- Include the C wrapper through `executables/edgeric/wrapper.h`.
- Report each nonempty logical channel's DL buffer through
  `ric_set_dl_buffer`, at the same point as EdgeRIC.
- Receive MCS commands before the first UE loop in `pf_dl`.
- Report smoothed `UE->dl_thr_ue`, integer-divided PUSCH SNR, and wideband CQI
  after the throughput update.
- Call `get_mcs_from_bler_new(UE->rnti, ...)` in the adaptive-MCS branch.
  Retain 2025.w34's configured-MCS cap and BLER-state update in the
  `harq_round_max == 1` branch.
- Report the PF single-RB TBS estimate using EdgeRIC's `tbs * 1.0000001`
  expression.
- Increment the TTI counter, send metrics, and poll for weights before
  allocating new transmissions, in EdgeRIC's order.
- Call `nr_find_nb_rb_new(rnti, ..., rb_total_num[beam.idx], ...)` for new
  transmission allocation.

The adaptation to 2025.w34 uses per-beam arrays for EdgeRIC's
`rb_total_num` and `first_time_flag`. Each beam records its first encountered
contiguous free span, immediately after the span search. With one beam this
uses the same budget rule as EdgeRIC. This adaptation is not a complete
multi-beam policy design: weights are still supplied by the existing
RNTI-based interface.

Preserved the newer connected-UE list, RLC calls, timers, PF sorting,
retransmission path, beam selection, BWP offsets, occupancy-map convention,
and PDSCH/DCI construction.

### Required next steps before rebuilding

The two helper definitions and matching header declarations are now present
(see section 9). Check executable linkage: scheduler references to `agent`,
`tti_counter`, and `ric_*` may require changes for other executables sharing
this MAC code, beyond the existing `nr-softmodem` link. Rebuild and validate
controller-present/controller-absent
scheduling, retransmissions, and resource accounting.

The downlink edit was reviewed against EdgeRIC and passed `git diff --check`
at the time it was made. No Docker build or runtime validation of the new
scheduler integration has been performed.

### Improvements deferred at the user's request

These are observations for later review, not changes made in this step.

| Area | Current EdgeRIC behavior | Possible later improvement |
| --- | --- | --- |
| DL buffer metric | Setter overwrites the UE entry once per nonempty logical channel; buffer maps persist between reports. | Report the per-UE aggregate once, including zero when drained. |
| SNR precision | `pusch_snrx10 / 10` truncates fractional dB. | Use `10.0f`; document that this is uplink PUSCH SNR. |
| `tx_bytes` meaning | Reports smoothed throughput, accumulated by the setter until metrics are sent. | Define units and distinguish raw bytes from smoothed throughput. |
| `dl_tbs` meaning | Reports a hypothetical single-RB PF estimate with a tiny multiplier. | Document the estimate, remove the multiplier if appropriate, and distinguish actual scheduled TBS. |
| TTI identity | Counter advances on `pf_dl` calls, not every radio slot. | Review absolute slot identity, controller alignment, counter overflow, and multiple MAC instances. |
| RB budget | First contiguous span becomes the reference budget, now separately per beam. | Define budgets for different BWPs, symbol allocations, fragmentation, and beam-specific policy normalization. |
| Weighted helper bounds | Original helper overwrites `nb_rb_max` with weighted budget. | Bound it by the current free span and remaining resources; validate weights and handle zero/below-minimum budgets before reserving control resources. |
| Weighted helper search | Original helper comments out the final `*nb_rb = hi`. | Restore/review binary-search output and validate TBS against requested bytes and RB limits. |
| External MCS | Original helper narrows the integer result to `uint8_t`, uses 255 as a sentinel, and overrides MCS without a final limit check. | Preserve the integer sentinel and validate against the active table/configured limits. |
| MCS timing | Original helper applies overrides only on BLER-update opportunities; the no-retransmission branch bypasses it. | Decide explicitly whether external MCS should apply every scheduling opportunity. |
| Policy availability | Existing receive functions poll nonblocking and clear stored policy when no message arrives. | Define policy lifetime and fallback behavior; do not assume a response to the just-sent report is already available. |

The helper issues above remain in the EdgeRIC helpers now copied into the
SRK working tree, as requested. Keeping the port close to
EdgeRIC does not establish the correctness of those existing behaviors.

## 9. Scheduler primitives and helper declarations

Added EdgeRIC's two complete helper definitions while retaining the original
2025.w34 helpers for existing callers. Both new bodies match EdgeRIC's
implementation, ignoring comments and whitespace; improvements remain deferred.

| File / change | Current working-tree location |
| --- | --- |
| `gNB_scheduler_primitives.c`: `<float.h>`, `<stdint.h>`, and wrapper includes | [Line 33](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c:33) |
| `nr_find_nb_rb_new`: applies an external weight to the reference RB budget | [Line 701](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c:701) |
| `get_mcs_from_bler_new`: retains EdgeRIC's BLER logic and external MCS override | [Line 903](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_primitives.c:903) |
| `mac_proto.h`: matching RB-helper declaration | [Line 456](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/mac_proto.h:456) |
| `mac_proto.h`: matching MCS-helper declaration | [Line 470](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/mac_proto.h:470) |

`<stdint.h>` precedes the wrapper because it uses `uint16_t` and `uint32_t`
without including their defining header. This fixes the include-order error
found during review. These additions account for all EdgeRIC changes to
`gNB_scheduler_primitives.c`; no other changes to that file are needed for
the faithful port. Source review is complete, but full compilation/linking
and runtime behavior remain unverified.

## 10. Uplink scheduler: EdgeRIC telemetry

Reviewed the user's edits to `gNB_scheduler_ulsch.c`: all three functional
EdgeRIC additions are present at the equivalent 2025.w34 locations.

| Change | Current working-tree location |
| --- | --- |
| Include the EdgeRIC C wrapper | [Line 39](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c:39) |
| Report `UE->ul_thr_ue` immediately after updating it in `pf_ul`, before clearing current statistics | [Line 1978](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c:1978) |
| Report `sched_ctrl->sched_ul_bytes` after incrementing it, inside the initial-transmission branch of `post_process_ulsch` | [Line 2437](/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g/openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_ulsch.c:2437) |

This preserves EdgeRIC's metric meanings: `rx_bytes` receives the smoothed
throughput variable, and `ul_buffer` receives scheduled-byte accounting,
not estimated UE queue occupancy. Uplink MCS selection and resource
allocation retain the existing OAI helpers; EdgeRIC adds telemetry only in
this file. The newer beam, timing, and HARQ code remains intact. The scoped
`git diff --check` passed; no build or runtime test was performed.

## Docker rebuild sequence

Check executable linkage when rebuilding
the current working tree. The commands below record the earlier successful
three-stage build sequence.

The following commands reproduce the three-stage sequence for the current
DGX Spark setup, using the Dockerfiles' default CUDA base image. The existing
OAI `host_product_family` file must identify the intended platform, as in the
successful build.

Run in Bash. Variables are set inside the block so a new terminal or an
earlier subshell cannot leave the image tag empty:

```bash
(
  set -euo pipefail
  cd ~/mec-offload-ad/gnb-core
  build_tag=edgeric-v1
  oai_dir="$HOME/mec-offload-ad/gnb-core/ext/openairinterface5g"

  docker build --progress=plain \
    --target ran-base-cuda \
    --tag "ran-base-cuda:$build_tag" \
    --file "$oai_dir/docker/Dockerfile.base.ubuntu.cuda" \
    "$oai_dir" \
    2>&1 | tee build-edgeric-base.log

  docker build --progress=plain \
    --build-arg DOCKER_CUSTOM_IMAGE_TAG="$build_tag" \
    --target ran-build-cuda \
    --tag "ran-build-cuda:$build_tag" \
    --file "$oai_dir/docker/Dockerfile.build.ubuntu.cuda" \
    . \
    2>&1 | tee build-edgeric-build.log

  docker build --progress=plain \
    --build-arg DOCKER_CUSTOM_IMAGE_TAG="$build_tag" \
    --build-arg DOCKER_CUSTOM_BASE_IMAGE_TAG="$build_tag" \
    --target oai-gnb-cuda \
    --tag "oai-gnb-cuda:$build_tag" \
    --file "$oai_dir/docker/Dockerfile.gNB.ubuntu.cuda" \
    "$oai_dir" \
    2>&1 | tee build-edgeric-gnb.log
)
```

`pipefail` prevents `tee` from masking a failed Docker command. An empty
`build_tag` caused the observed `invalid tag "oai-gnb-cuda:"` error; setting
the variable again fixed that command before Docker began building.

After a source-only change, rebuild **build → gNB** using the same existing
base tag. After changing base dependencies, rebuild **base → build → gNB**.

## Deferred work and limits

- These Docker stages do not invoke neural-receiver plan generation.
  `build-trt-plans.sh` still contains `trtexec --fp16`; TensorRT 11 plan
  generation needs a separate precision/model migration. That script was
  not edited. See the [NVIDIA migration guide](https://docs.nvidia.com/deeplearning/tensorrt/latest/api/migration/tensorrt-10x-to-11x-trtexec.html).
- The receiver's `tutorial.yaml` container build command still selects
  `ran-build-cuda:latest`, not `ran-build-cuda:edgeric-v1`. Select a matching
  image when generating plans for the new runtime.
- Neural receiver smoke tests, numerical behavior, CUDA-graph execution,
  and live gNB performance remain separate runtime checks.
- Scheduler hooks now reference EdgeRIC from shared MAC code. Check linkage
  for other executables as part of validating the scheduler port; the earlier
  successful image builds did not include these hooks.
- The uplink telemetry and helper/header additions are complete at the
  source level; build/link and runtime validation remain outstanding.

When saving this work, commit the OAI edits inside its submodule and push
that commit first. Then commit the updated submodule pointer, the neural
receiver edit, and this document in the parent repository.
