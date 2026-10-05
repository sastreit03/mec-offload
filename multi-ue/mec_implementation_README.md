# MEC architecture implementation

This is a new implementation alongside the original HTTP/VLM demos. It uses
Python 3.10+ (tested with 3.12), one asyncio control process and a bounded pool
of spawned model processes. It does not require FastAPI, a dashboard or a RIC
to run. The comments explain the ownership and lifecycle rules where they matter.

## Run the CPU example

From this directory:

```bash
python -m pip install -r requirements.txt
python -m mec.server --config config.yaml
```

In another terminal:

```bash
python -m mec.ue --frames 60
```

For a second independent UE, use separate identity/log files:

```bash
python -m mec.ue --frames 60 --state ue2.json --log ue2_events.jsonl
```

The default catalog uses CPU mock inference and fragmented JPEG over RTP. This
tests the complete lifecycle without CUDA/GStreamer or model downloads. JPEG
payload type 97 is a **private test format**, not standard RFC 2435 JPEG RTP.
The H.264 path uses standard RFC 6184 payloads with our negotiated RTP extension.

For your testbed, set `network.advertise_host` to the reachable MEC address
(e.g. `192.168.72.136`). Keep control TCP 8765 and expose/route the configured UDP
media range. In the UE container, for example:

```bash
python -m mec.ue --url ws://192.168.72.136:8765/ws/ue --ip 12.1.1.2 --frames 300
```

Each accepted task gets a listening UDP port **before** its acceptance response.
The UE may immediately stream to it. `status: preparing` is always present in
acceptance; it is not a claim that inference is already ready.

## Files and ownership

| File | Responsibility |
|---|---|
| `mec/config.py`, `tasks.yaml` | Pydantic-validated server limits and catalog combinations |
| `mec/control.py` | Per-UE WebSockets, resume tokens, connection IDs, results and replay |
| `mec/records.py` | Serializable UE/task rows and request/SLO schema |
| `mec/decision.py` | Heuristic, optional RIC decision/identity HTTP bridge, gRPC telemetry |
| `mec/service.py` | Admission reservations, task timers, worker assignment and frame dispatch |
| `mec/workers.py` | Spawned reusable processes, fixed CUDA streams and one job in flight |
| `mec/adapters.py` | Model preparation, warmup, inference and state reset |
| `mec/frames.py` | The bounded decoded-frame queue and replaceable FIFO policy |
| `mec/rtp.py`, `mec/media.py` | RTP metadata, JPEG assembly and GStreamer H.264 reception |
| `mec/runlog.py` | Background JSON/JSONL logging |
| `mec/ue.py` | Matching UE implementation and local latency measurements |

CUDA is initialized only in model workers, using `spawn`. Reception and control
continue while models load or run. A worker stays assigned to a task for its
whole video, then resets tracking state and retains its warm model for reuse.
Two tasks selecting the same configuration get separate processes/replicas.
Compatibility currently requires the exact catalog `config_id`; shared-memory
IPC and wider cross-configuration reuse are future optimizations.

## Queues, admission and completion

- The default frame cap is **8 waiting decoded frames**, excluding the single
  submitted job. Overflow evicts the oldest waiting frame even when the UE sets
  `can_drop_frames: false`. Every eviction receives a logged/result outcome.
- FIFO dispatch is a small policy class selected by `queues.frame_policy`.
  There is no discretionary deadline-based skipping in this version.
- Up to **5 accepted tasks** may be waiting or preparing. They continue counting
  until execution-ready. Running tasks are separately bounded by worker slots.
- Worker assignment is FIFO among compatible queued tasks. All compatible warm
  assignments happen before idle replicas are evicted for another model.
- A compatible idle worker can take a new task directly if older compatible
  waiters have already been served. Otherwise a full waiting queue produces
  `accepted: false`, `reason: capacity_limit` before acceptance.
- Admission and deduplication are serialized in one short control-side critical
  section, including the bounded RIC call and receiver setup. GPU work is never
  inside this lock. Queued preparation keeps receiving media.
- `task_end` contains `final_frame_id`. Frame IDs begin at zero and increase
  without intentional sender-side gaps; `-1` denotes an empty stream. End may
  arrive before final media. Arrival grace and drain have separate deadlines.
- Completely missing frames are reported as missing-ID ranges, since their
  capture timestamps cannot be recovered. Received frames have separate job
  records. A stream may complete **with missing frames**; this is not successful
  processing of every transmitted frame.
- Disconnect grace continues reception and inference for 5 seconds. On expiry
  reception and new submissions stop. Submitted work finishes before reuse.
  Reconnection does not revive an expired task.

The example decoded-memory guard reserves `(8 + 2) × width × height × 3` bytes
per task for waiting frames, one submitted RGB frame and one decoder/callback
staging frame. It is a reservation estimate, not an OS memory limiter. IPC can
temporarily copy one submitted frame. Socket, encoded assembly, codec reference
pictures, Python objects and GPU memory are separate, documented allocations.
JPEG assembly is also bounded by count, encoded frame size and aggregate bytes.
If a GPU memory budget is supplied, configurations require peak-memory
estimates; the pool reserves their sum. Actual OOM/preparation errors still
produce failures, so replace example estimates with measured values.

## Configured timers

| Timer | Initial seconds | Boundary / outcome |
|---|---:|---|
| Worker wait | 10 | Acceptance until compatible worker or preparation slot; `resource_timeout` |
| Preparation | 15 | Actual worker start until ready; `preparation_timeout` / `preparation_failed` |
| Overall readiness | 25 | Acceptance until ready; `readiness_timeout` |
| First valid media | 5 | Acceptance until a valid task packet; `no_input_timeout` |
| Final arrival grace | 0.5 | `task_end` until complete input or grace expiry |
| Drain | 5 | Input closure until queued/submitted work finishes; `drain_timeout` |
| Control disconnect | 5 | Detected disconnect until same-identity recovery |
| RIC decision | 1 | RIC call until response; heuristic fallback |

These are retention/startup timers, not frame SLO guarantees. All are in YAML.
Timeout/failure after acceptance produces `task_expired`/`task_failed`, never a
retroactive denial. The first observed terminal condition wins. A task cannot
return to active after expiry, including when a late job result arrives.

## H.264 and the actual buffering settings

Install GStreamer on MEC and UE using `scripts/install_media_deps.sh` in an
Ubuntu image/host, then use a Python interpreter with matching GI bindings.
On Ubuntu 24.04, a system Python 3.12 venv created with
`python3 -m venv --system-site-packages .venv` can see apt-installed GI. Do not
assume an unrelated Conda Python version can import those compiled bindings.

```bash
python -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
python -m mec.ue --task-type frame_statistics_h264 --frames 60
```

Receiver pipeline:

```text
appsrc -> rtpjitterbuffer -> rtph264depay -> h264parse -> avdec_h264
       -> videoconvert -> RGB caps -> appsink -> application FrameQueue
```

There is no tee, preview branch or extra `queue` element. Selected settings:

| Stage | Explicit settings |
|---|---|
| UDP socket | Requested receive buffer 2 MiB; effective OS value recorded |
| RTP appsrc | `max-buffers=256`, `max-bytes=0`, `max-time=0`, `block=true`, nonleaky |
| RTP jitterbuffer | `latency=20 ms`, `drop-on-latency=true`, `do-lost=true`; drop messages enabled if supported |
| H.264 depayloader | Wait for a keyframe after transport loss when supported |
| Decoded appsink | `max-buffers=1`, `drop=false`, `sync=false`, `wait-on-eos=false`, `enable-last-sample=false`; optional byte/time limits disabled, nonleaky |
| Application queue | 8 frames; counted oldest-frame overflow |
| UE encoder | No B-frames, zero lookahead, one encoder thread, periodic keyframes, `tune=zerolatency` |

The 256-buffer appsrc cap is **packets**, not decoded frames, and is explicit
additional staging. If it blocks, the socket can lose packets; this is not a
silent decoded-frame skip policy. Jitter drops/loss, assembly loss, metadata
eviction, application overflow and media errors are logged separately.
`events.jsonl` records installed Gst version, pipeline text, effective supported
properties, and actual socket buffer size for every receiver. Decoder reference
pictures and OS buffers still exist. No claim is made that all buffering vanishes.

Frame metadata is extracted before depayloading, mapped from RTP timestamp to
jitterbuffer PTS, and looked up at the decoded appsink. Unknown metadata is
logged rather than assigned a made-up ID. H.264 `frame_complete_receive` is the
last observed packet associated with the decoded access unit; it does not prove
that no packet was lost or that the decoder did not conceal damage.

## CUDA and MPS on Spark

Use your working CUDA-compatible PyTorch/NVIDIA container, install the core
requirements plus `requirements-gpu.txt`, and download checkpoints in advance.
Copy/adapt `tasks.gpu.example.yaml` to `tasks.yaml`; configure replica counts,
worker limits, reachable addresses and measured memory values for the deployment.
For the GPU run set `pool.require_mps: true`.

Start an MPS daemon **before** model workers, using your chosen
`CUDA_MPS_PIPE_DIRECTORY`/`CUDA_MPS_LOG_DIRECTORY` and CUDA device visibility.
This application does not start/stop a shared host daemon or alter SM partition
settings. Each worker sets `CUDA_MPS_CLIENT_PRIORITY=0` before CUDA initialization.
The initial check uses NVIDIA's legacy MPS `get_server_list` / `get_client_list`
interface. A newer MPS-v3-only installation requires adapting `mps_clients()`
to its `client list` interface. Containers must share the intended daemon pipe,
GPU, user permissions and PID namespace for this PID check (e.g. host PID
namespace); do not interpret a different PID namespace as proof of no MPS.

Workers query the device priority range and create fixed HIGH/LOW streams.
The selected task stream remains fixed throughout the video. No stream priority
mutation, scheduler GPU preemption or exclusive-compute reservation is used.
Model adapters must respect the selected stream or explicitly synchronize any
auxiliary streams. Stream priority is a hint, not a strict order guarantee.

```bash
python -m scripts.check_gpu_pool --config config.yaml
```

This explicit check loads two CUDA replicas, verifies their MPS client PIDs,
reports the actual priority range, and submits HIGH/LOW jobs concurrently.
It does not demonstrate kernel overlap or certify performance isolation.

## Control and media contracts

`mec_hello` contains `ue_id`, `resume_token`, `connection_id`, `run_id`, catalog
version and complete supported configurations. The UE stores the token and
sends `X-MEC-Resume-Token` on reconnect. One live connection per UE is allowed;
a recovered connection explicitly replaces the older one.

Request example:

```json
{"type":"offload_request","ue_id":"from-hello","client_seq":0,
 "ip_address":"12.1.1.2","task_type":"object_detection","input_type":"video",
 "slo":{"latency_ms":200,"latency_origin":"capture","accuracy":null},
 "can_drop":false,"can_drop_frames":false,"parameters":{}}
```

Acceptance echoes IDs and includes `accepted: true`, `status: preparing`, task
type, complete app configuration, fixed task priority, endpoint, media token,
RTP metadata layout and overflow rules. There is no task_ack or required ready
handshake. Retry `(ue_id, client_seq)` returns the original task and admission
response plus `current_status`; changing semantic fields with that key is an
error. Updated IP/retry timestamp alone does not create another request.

The RTP extension is RFC 8285 profile `0x1000`, two-byte extension ID 1. Its
57-byte body is network byte order: version u8, task UUID 16 bytes, media token
16 bytes, frame ID u64, capture Unix ns u64, transmission-start Unix ns u64.
It is repeated on every packet, with packet size kept under the negotiated MTU.
The token/task identity prevents stale packets entering a reused port; it is not
an encrypted/authenticated media protocol.

Results use a common `frame_result` envelope with UE/task/frame/job IDs, stable
`result_id`, ordered `result_seq`, status/reason, timing and adapter-owned JSON
`payload`. Both WebSocket peers use `max_size=None`; no application payload or
replay byte cap is imposed. JSON/nonfinite validation still applies.

All result/lifecycle payloads remain in memory for this run. Reconnect
conservatively replays **all retained results**, including uncertain or already
written deliveries; UE stable-ID suppression prevents duplicate consumption.
No result acknowledgment was added. This uses more RAM over long runs, as
expected from the agreed uncapped replay policy. Server restart restores nothing.

## Decisions and RIC integration

The baseline chooses the first matching catalog entry and LOW priority. It
does not favor a currently idle model, certify the latency/accuracy requirement,
or implement an RL agent. Catalog order is an explicit simple baseline choice.
Task metrics are arbitrary dictionaries; profiles remain optional. Admission
without fresh telemetry still enforces configuration, queue and memory guards.

The optional RIC bridge expects HTTP POST at configured `decision_url`:

```json
{"task_id":"request-task-id","plan":{"accepted":true,
 "config_id":"yolo11n-detect","priority":"high","reason":null}}
```

Inputs contain the task/request, catalog, queue/worker state and telemetry.
Malformed, unavailable, incompatible or late replies fall back to the heuristic.
The identity endpoint receives application UE ID, IP, connection ID and update
time; RAN identity association remains at the RIC. No UE row contains an RNTI.
The RIC HTTP endpoints themselves must be provided by your xApp/bridge; they are
not assumed to already exist in the current telemetry-only xApp.

Optional telemetry uses the existing generic gRPC
`/ran.telemetry.RANTelemetry/StreamTelemetry` JSON stream. Add a **source**
`report_unix_ns` to snapshots and align `reporting_period_s` and period origin.
Current/previous source periods are fresh; absent timestamps remain unknown/stale
and do not block admission. RNTI fields are stripped before MEC caching/logging.

## Measurements and saved records

Each run has `run.json`, `jobs.jsonl`, `events.jsonl`. The snapshot stores the
actual settings/catalog, UE/task rows, pending jobs, a bounded recent execution
history and worker state. Resume tokens, sockets, queues and frame arrays are
excluded. Large result payloads are not written to experiment logs; compact
payload metadata is recorded instead. Full payloads remain available for delivery.

Every received frame's execution/drop record preserves its phase timestamps.
MEC logs reception/decoding/queue/processing boundaries, CUDA submission and
observed completion, terminal times and per-send delivery events. Since execution
records are immutable, result-send/replay timestamps are joined from events by
task/frame/result identity. Task rows retain counts, first/last timing and log refs.

`gpu_start_time` and `gpu_finish_time` are explicitly **host submission / observed
completion**, not exact kernel boundaries. `cuda_event_ms` measures the stream
interval around inference; CPU work/stream idle gaps can be inside that interval.
Profiler traces are required for actual per-kernel start/end times. CPU jobs have
null GPU fields.

UE logs request-send/response-receive and frame-transmission/result-receive.
The demo capture boundary is the frame's availability/release in the UE app,
before encoding; prerecorded video uses this run's release time. It computes
capture-to-result latency locally, without comparing clocks across hosts.
Cross-host phase subtraction still requires synchronized wall clocks. MEC SLO
completion remains unknown until joined with UE receipt; dropped/failed outcomes
are explicitly unsuccessful, not silently excluded from compliance.

Snapshots are atomic every 5 seconds, and best-effort on SIGINT/SIGTERM/handled
failure. JSONL records are line-flushed by a background writer; snapshots are
fsynced. An unrecoverable crash can lose queued/OS-cached records. Logging files
are experiment data, not restart recovery.

## Verification and scope

```bash
python -m unittest discover -s tests -v
```

Tests cover real WebSocket/UDP traffic and spawned CPU workers: separate UEs,
same-model replicas, warm reuse, duplicate/conflicting requests, early media,
overflow, missing frames, preparation/resource/input/drain timeouts, worker
failure, reconnect/replay, RIC fallback, malformed control and matching UE logs.
The H.264 roundtrip test runs automatically when GI is installed; otherwise it
is explicitly skipped. CUDA/MPS validation uses the hardware check above.

This first version includes the model-agnostic platform, mock and YOLO
detection/classification/tracking adapters. Detection+recognition pipelines,
segmentation payload definitions, temporal-window adapters, measured accuracy
profiles and intelligent scheduling are extensions behind those same interfaces.
The original demo files and deployment scripts are unchanged. To use its Docker
setup, copy this package/config/catalog into the image, install requirements and
change its entry point to `python -m mec.server --config config.yaml`.

API references used for this implementation:

- [PyTorch CUDA streams](https://docs.pytorch.org/docs/stable/generated/torch.cuda.Stream.html)
- [NVIDIA MPS environment settings](https://docs.nvidia.com/deploy/mps/appendix-environment-variables.html)
- [NVIDIA MPS client-list interface](https://docs.nvidia.com/deploy/mps/appendix-tools-and-interface-reference.html)
- [GStreamer appsink](https://gstreamer.freedesktop.org/documentation/app/appsink.html)
- [GStreamer appsrc](https://gstreamer.freedesktop.org/documentation/app/appsrc.html)
- [RFC 8285](https://www.rfc-editor.org/rfc/rfc8285.html)
- [Ultralytics prediction](https://docs.ultralytics.com/modes/predict/)
