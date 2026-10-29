# Multi-UE heuristic baseline

The fixed-policy campaign and SRK trace-bank workflow are documented in
[`docs/data-collection-mode.md`](docs/data-collection-mode.md) and
[`docs/channel-emulation.md`](docs/channel-emulation.md).

This is an additive implementation bundle for the supplied testbed. Place `multi-ue/` and `EdgeRIC-v2/muApp_mec/` beside the existing `gnb-core/`. It contains no OAI source replacement and does not use `docker-compose.override.yaml`. Keep the repaired OAI image and current SRK launches.

The MEC owns admission, joint GoP selection, frame priority, execution and results. EdgeRIC owns ephemeral radio associations and demand/weights. The UE owns encoding, actual configuration application, transmitted-frame evidence and complete-result receipts. RL interfaces are prepared; no RL feature set, reward, algorithm or training scheme is selected.

## Included code

| Path | Purpose |
|---|---|
| `contracts/` | Versioned protobuf envelope, 96-byte RTP metadata, JSON validation |
| `mec/src/mec_server/` | WebSocket control, registries, GStreamer reception, estimators, heuristic policies, persistent CUDA workers, tracking, replay |
| `ue/android/` | Pixel application, camera/dataset selection, cellular-bound transport, GoP commands, probes and evidence |
| `ue/linux/` | Camera/file client in the existing softUE network namespace |
| `../EdgeRIC-v2/muApp_mec/` | Standalone muApp, existing ZMQ wire adapter, association, demand, telemetry and UL utility |
| `config/` | Validated catalog, separate synthetic development settings, measured-profile template, shadow configuration |
| `scripts/`, `mec/docker/` | Build/start/stop, MPS/GPU checks, independent compose files |
| `tools/` | Export, profiling-data import and all-transmitted-frame evaluation |
| `tests/`, `docs/` | Replay/integration tests, contracts, source provenance and acceptance gates |

## Current validation boundary

Read `docs/validation.md` before experimental use. CPU tests exercise real local gRPC, WebSocket control and three spawned **mock** workers. They establish software behavior, not OTA, CUDA concurrency, camera performance or experimental quality.

Experimental startup deliberately rejects missing measured timing, quality, residence, network and budget settings. `profiles.synthetic.yaml` is development evidence only. No pretrained weights or datasets are included. The default experiment is the documented 1280×720/30-FPS/30-frame-GoP workload. `fps`, `gop_frames`, `applications`, `priority_streams`, and `keyframe_interleaving.enabled` are validated YAML controls; changing FPS or GoP length also requires matching catalog and profile evidence. The priority-stream list contains one to three ordered CUDA levels, and its length selects the stream count. Both clients select camera versus dataset at runtime and request the MEC-advertised input profile.

A material source limitation remains: the supplied scheduler trace flushes approximately once per second. It provides grant evidence, but does not establish fresh 100-ms counter coverage. Current metrics contain smoothed scheduling throughput, not confirmed application delivery. Exact R53 capacity context and fresh telemetry must pass the live/source gate before enabling experimental heuristic weights. The code does not pretend those measurements are available. See `docs/telemetry.md`.

## Local checks

```bash
cd multi-ue
make bootstrap
make test
source scripts/environment.sh
python -m mec_server.app --config config/mec.development.yaml
```

The development MEC uses synthetic profiles and mock receivers: it intentionally does not accept real video. Run `python -m pytest tests/test_integration.py -v` to exercise early descriptors, demand, mock processing, reconnect fencing, replay and missing frames. This is not a video demo or a performance result.

## Prepare the MEC Spark

1. Retain the existing core containers/network setup. Stop the previous demo MEC container occupying its address/port. Use `scripts/containers.sh start-mps` on the MEC host before workers. This uses that host's MPS service, separately from the RAN Spark.
2. Supply local task-appropriate checkpoints under a checkpoint directory. Run `tools/export_models.py --checkpoints PATH --output weights` in the target NVIDIA environment. Detection and tracking load separate copies. SegFormer checkpoints must have the intended class head/map; quality qualification is independent of successful export.
3. Profile with the core stack running. Populate the measured profile file and required settings in `config/mec.experimental.yaml`. `tools/build_profiles.py --help` describes the joined measurement CSV. Do not copy synthetic values into an experimental file.
4. Set a fresh shared `run_id` for each measured run; retain prior run directories. Configure reachable addresses, model/profile hashes and calibrated budgets. Initial example addresses follow the supplied source, not a discovery guarantee.
5. Build/start with `scripts/containers.sh build-mec` and `scripts/containers.sh start-mec`. The independent compose file attaches to `oai-traffic-net`/`oai-public-net` and installs the existing UE-subnet route via UPF. Verify `/readyz` and `scripts/containers.sh gpu`; inspect worker/model/stream startup records.

For initial live-path validation before measured budgets are available, start the
same real-worker image with the explicit calibration configuration:

```bash
MEC_CONFIG=mec.calibration.yaml scripts/containers.sh start-mec
```

## Prepare the RAN Spark

Keep using the supplied SRK gNB/core launch workflow and rebuild its repaired OAI image with the Aetheredge scheduler changes. Capacity context and cumulative new-data/retransmission grant counters now travel from OAI to EdgeRIC on localhost ZMQ; the scheduler CSV is optional offline diagnostics and is not consumed by the muApp.

Stop any old muApp or container publishing weights on localhost:5556 before starting this muApp. Exactly one publisher may own that port. The EdgeRIC container shares the gNB network namespace so it can subscribe to localhost:5555 without a trace-volume mount.

```bash
cd multi-ue
scripts/containers.sh build-edgeric
scripts/containers.sh start-edgeric
```

The new container shares `container:oai-gnb` networking, subscribes to localhost:5555, and serves gRPC on the gNB IPvlan address at port 50052. No E2 service model is used. With `mode: shadow`, it computes/logs proposed weights and **sends no weight messages**. Changing to `heuristic` is a later startup configuration after the telemetry/capacity and shadow gates pass. `ran_index` is echoed as in the existing implementation; strict k/k+1 deadline enforcement remains deferred by user instruction.

## UE launch

Android: open `ue/android` in Android Studio with JDK17/Android35, build/install the debug app, grant camera permission, enter the reachable MEC address, select detection/tracking/segmentation, then choose rear camera or a dataset directory using the document-tree picker. A directory is shuffled with a recorded seed, every video runs once per cycle, and the next prerecorded session is prepared before EOF. Camera mode remains a single continuous session. Keep the application in the foreground. It requests the cellular network and binds control, media and probe sockets to it. No UE IP or RNTI is entered manually. Its JSONL evidence is in the application's external-files directory; pull it with `adb` for reconciliation. Native UI displays result status/schema; image overlays are not implemented.

Linux softUE: install Python/GStreamer/OpenCV dependencies and mount this source at `/multi-ue` in the existing softUE container. Run:

```bash
scripts/softue-client.sh --source /datasets/clip.mp4 --application detection --logs /multi-ue/runs/ue-a
# Continuous prerecorded playlist; omit --shuffle-seed for a random recorded seed.
scripts/softue-client.sh --source-dir /datasets --shuffle-seed 17 --application detection --logs /multi-ue/runs/ue-a
# Camera: --source 0
```

Mount the host dataset directory read-only at `/datasets` in the softUE container. The wrapper supplies the interface name, installs a host route for the MEC through that tunnel, and the client discovers and refreshes its IPv4 address automatically. Override `SOFTUE_CONTAINER`, `SOFTUE_INTERFACE` or `MEC_SERVER` as needed. This does not build or replace softUE or its B210 setup.

Association is a serialized experimental traffic-signature procedure, not a cryptographic or deterministic radio identity source. Ambiguous matches remain PF and retry. The per-run EdgeRIC mapping is generated automatically. IP/connection change, missing RNTI, lease expiry and source restart trigger invalidation/reprobe. Silent reuse of the same numeric RNTI is detected only within the configured revalidation/lease bounds; measure recovery with non-MEC/background traffic before experiments. Probe traffic affects offered load and must be marked in analysis.

## End and evaluate

End the video, allow drain plus the five-second missing-result reconciliation period, and preserve all UE/MEC/EdgeRIC logs and gNB traces. `tools/evaluate_ue.py UE_EVENTS.jsonl` uses **every transmitted frame** as the denominator. A later result cannot turn a missed 150-ms deadline into success. Local result serialization/send is not proof of UE delivery.

Stop the new containers with `scripts/containers.sh stop-mec` and `stop-edgeric`; the scripts do not stop the existing core/gNB. Then use `scripts/containers.sh stop-mps` after the MEC has exited. SIGTERM/Ctrl+C attempts a final snapshot; hard kills cannot guarantee log flushing.
