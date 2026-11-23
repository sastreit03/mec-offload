# MEC multi-UE system context

This repository, normally checked out at `~/mec-offload-ad`, implements a multi-access edge computing (MEC) testbed over a private OpenAirInterface (OAI) 5G network. UEs send encoded video to the MEC for detection, tracking, or segmentation and receive per-frame results. Experiments collect UE, MEC, and radio-controller evidence for latency and service-level objective (SLO) analysis.

## Devices and deployment

- **RAN Spark:** NVIDIA DGX Spark (`spark-763f`), running the OAI gNB (5G base station) and EdgeRIC radio controller. Campaign generation and orchestration are normally run here.
- **MEC Spark:** NVIDIA DGX Spark (`spark-9d60`), running the MEC server and OAI 5G core network. The two Sparks are connected by a **QSFP cable**.
- **Three Google Pixel 7 UEs:** aliases `pixel1`, `pixel2`, and `pixel3`; each runs the Android video client over its cellular connection.
- **NVIDIA Jetson Orin:** alias `linux1`; runs the OAI software UE and Linux video client. The client uses the software UE's cellular network interface.

The pilot campaigns use physical RF, without a channel emulator. The repository also contains channel-emulation support; use the selected campaign configuration to determine whether it is enabled.

## Repository directories

Paths below are relative to `mec-offload-ad/` and refer to the checkout on the relevant device.

| Directory | Current use |
|---|---|
| `multi-ue/mec/src/mec_server/` | MEC control, GStreamer video reception, frame scheduling, model workers, result delivery, and evidence recording on the MEC Spark |
| `EdgeRIC-v2/`, especially `muApp_mec/` | EdgeRIC implementation on the RAN Spark: gNB telemetry, UE-to-radio association, and radio-policy integration |
| `gnb-core/` | Used on **both Sparks**: gNB deployment on the RAN Spark and 5G-core deployment on the MEC Spark; includes the OAI source integration |
| `ue/` | OAI software-UE deployment used by the Orin |
| `multi-ue/ue/android/` | Android application built and installed on the three Pixels |
| `multi-ue/ue/linux/` | Linux application client used by the Orin, inside the software-UE network environment |
| `multi-ue/config/` | MEC settings, model catalog, measured profiles, and campaign YAMLs under `campaigns/` |
| `multi-ue/scripts/` and `multi-ue/tools/` | Deployment helpers, UE supervision, profiling, campaign generation/execution, and analysis |
| `multi-ue/runs/` | Device-local run evidence; campaign manifests/state and gathered diagnostics/results are under `runs/campaigns/<campaign>/` |

`gnb-core/` and the top-level `ue/` are separate working trees of the **Sionna Research Kit (SRK)** repository. Do not confuse top-level `ue/` (the cellular software UE) with `multi-ue/ue/` (the video applications).

## Operating modes and workflows

- **Collection (`mode: collection`):** Fixed-policy dataset campaigns. The manifest assigns participating UEs, applications, models, bitrates, and media. The MEC commands transmission start/stop and drains accepted work. Lateness alone does not discard frames. Current analysis selects valid attempts and frames whose first MEC packet arrival falls in **[1 s, 6 s)** after scenario release; the stop command is scheduled at 7 s. EdgeRIC normally runs in shadow mode while OAI retains proportional-fair scheduling.
- **Experimental (`mode: experimental`):** Live operation with heuristic admission, configuration selection, and frame-priority policies. Requires measured profiles and calibration; synthetic profiles and mock inference are rejected.
- **Development (`mode: development`):** Software testing with synthetic profiles and mock workers/receivers. This does not establish real-video, radio, or GPU performance.
- **Profiling:** A separate measurement workflow for model processing, encoder behavior, frame sizes, and application quality. Tools and client profiling functions produce measurements used to build profile YAMLs. **`profiling` is not a supported MEC `mode` value.**

Enabled applications, FPS/GoP, CUDA priorities, MPS use, media paths, campaign ID, and MEC `run_id` are configuration choices, not fixed hardware properties. Verify the active YAML and deployed build rather than assuming a previous campaign's settings. A campaign ID identifies its manifest; the MEC `run_id` identifies its run evidence and may differ.

For procedures, see [dataset-collection-tutorial.md](dataset-collection-tutorial.md). For implementation details, see [mec_frame_flow_memory.md](mec_frame_flow_memory.md) and [mec_frame_flow_queues.md](mec_frame_flow_queues.md).
