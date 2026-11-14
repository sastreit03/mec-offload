# MEC implementation diagrams

Audited 2026-11-02 from `multi-ue/mec/src/mec_server/`. These diagrams show
the implemented concurrency boundaries and call paths. They describe both
collection mode and the adaptive code that remains available in development
or experimental mode. The active collection campaign fixes the candidate per
UE assignment and CUDA priority at `-3`.

## Process and concurrency architecture

```mermaid
flowchart TB
  subgraph External["External actors"]
    RUNNER["Campaign runner"]
    UE["UE control client"]
    RTPIN["UE RTP/H.264 sender"]
    EDGE["EdgeRIC Awareness service"]
  end

  subgraph Main["MEC main process"]
    UV["Uvicorn<br/>one ASGI worker"]

    subgraph App["app.py / FastAPI"]
      HTTP["HTTP routes<br/>healthz, readyz, api/status,<br/>scenario arm/start/disarm"]
      WSH["WebSocket handler /ws/ue<br/>per-connection sender + replay tasks"]
      PR["ProbeReceiver<br/>asyncio UDP :8766"]
      LIFE["ASGI lifespan<br/>Engine.start / Engine.close"]
    end

    subgraph Engine["engine.py / Engine: asyncio-loop authority"]
      REG["Registries and lifecycle state<br/>UE, Video, Frame, dedup,<br/>scenario assignments"]
      CMD["command()<br/>association, scenario ACK,<br/>video_start, video_end"]
      TICK["tick() every about 2 ms<br/>drain events, poll workers,<br/>dispatch queues, enforce lifecycle"]
      EVQ["bounded thread-to-loop queue<br/>descriptor, progress, decoded,<br/>failure and pipeline events"]
      PEND["per-application / priority FIFO deques"]
      AWAREQ["bounded awareness PriorityQueue<br/>revisioned incremental messages"]
      AWT["send_awareness()<br/>authoritative sync + unary updates"]
      RPT["receive_reports()<br/>radio windows + probe commands"]
      RES["resource_reports()<br/>host queues, in-flight jobs, NVML"]
      TRACK["per-video Tracker state<br/>serialized through asyncio.to_thread"]
      RESULT["resolve() / deliver()<br/>one terminal outcome per frame"]
    end

    subgraph Media["media.py / one Receiver per admitted video"]
      GST["GStreamer pipeline<br/>udpsrc, jitterbuffer, depay,<br/>H.264 decode, RGB appsink"]
      META["pre-jitter RTP pad probe<br/>descriptor + unique-byte Assembly"]
      DECODE["appsink callback<br/>copy decoded RGB"]
    end

    WM["workers.py / Workers<br/>spawn context, process queues,<br/>busy stream reservations"]
    GPUSTAT["gpu.py / DeviceGpuSampler<br/>whole-device NVML snapshots"]
  end

  subgraph Children["Three persistent child processes"]
    DP["detection worker process<br/>4 frozen YOLO models"]
    TP["tracking detector process<br/>4 frozen YOLO models"]
    SP["segmentation worker process<br/>4 frozen SegFormer models"]
    DPOOL["thread pool<br/>one slot per configured CUDA stream"]
    TPOOL["thread pool<br/>one slot per configured CUDA stream"]
    SPOOL["thread pool<br/>one slot per configured CUDA stream"]
    MPS["CUDA streams through host MPS"]
  end

  subgraph Persistence["Disk and evidence boundaries"]
    RECQ["Recorder bounded queue"]
    RECT["single Recorder writer thread"]
    JSON["events.jsonl, jobs.jsonl, run.json"]
    REPLAY["ReplayStore SQLite WAL<br/>full frame-result payloads"]
  end

  RUNNER -->|"HTTP JSON"| HTTP
  UE <-->|"WebSocket JSON envelopes"| WSH
  RTPIN -->|"UDP RTP/H.264"| GST
  UE -->|"UDP signature packets"| PR
  EDGE <-->|"gRPC Awareness"| AWT
  EDGE -->|"server-stream reports"| RPT

  UV --> HTTP
  UV --> WSH
  UV --> LIFE
  HTTP --> REG
  WSH --> CMD
  WSH --> REG
  PR --> AWAREQ
  LIFE --> TICK
  LIFE --> AWT
  LIFE --> RPT
  LIFE --> RES

  GST --> META
  GST --> DECODE
  META --> EVQ
  DECODE --> EVQ
  EVQ --> TICK
  TICK --> REG
  TICK --> PEND
  PEND --> WM
  WM --> DP
  WM --> TP
  WM --> SP
  DP --> DPOOL --> MPS
  TP --> TPOOL --> MPS
  SP --> SPOOL --> MPS
  DP -->|"completed / failed"| WM
  TP -->|"detections"| WM
  SP -->|"class-map RLE"| WM
  WM --> TICK
  TICK --> TRACK
  TRACK --> RESULT
  TICK --> RESULT

  CMD --> AWAREQ
  TICK --> AWAREQ
  RES --> AWAREQ
  GPUSTAT --> RES
  AWAREQ --> AWT
  RPT --> REG
  RPT --> WSH

  RESULT --> RECQ
  REG --> RECQ
  RECQ --> RECT --> JSON
  RESULT -->|"asyncio.to_thread"| REPLAY
  REPLAY -->|"bounded reconnect pages"| WSH
  RESULT --> WSH
```

The critical ownership rule is that the asyncio loop owns `ues`, `videos`,
`frames`, pending queues, and lifecycle transitions. GStreamer callbacks and
worker processes never mutate those registries directly. They report through
bounded queues, and `Engine.tick()` applies the changes on the loop.

## Scenario and control lifecycle

```mermaid
sequenceDiagram
  autonumber
  participant R as Campaign runner
  participant A as FastAPI routes
  participant E as Engine
  participant W as UE WebSocket handler
  participant U as UE client
  participant X as EdgeRIC

  U->>W: Open /ws/ue with optional resume token
  W->>E: hello(resume token, peer IP, enqueue)
  E-->>W: hello envelope, UE ID and resume token
  E->>X: association update through awareness queue
  W-->>U: hello
  U->>W: association_update(alias, UE IP, clock map)
  W->>E: command()
  E->>X: revisioned association update
  E-->>U: association_pending

  R->>A: POST /api/scenario/arm
  A->>E: arm_scenario(context, assignments)
  E->>E: require idle; reset attempt state
  E->>X: authoritative sync requested
  E-->>U: scenario_context with alias assignment
  U->>W: video_start(client_seq, assignment IDs)
  W->>E: command() with client-sequence dedup
  E->>E: start_video; validate assignment/candidate
  E->>X: session and demand expectation
  E-->>U: admission response and RTP port
  U->>U: prepare media and hold first frame
  U->>W: scenario_context_ack(video_id)
  W->>E: validate context, admitted video and connection generation
  E-->>U: scenario_context_acknowledged

  R->>A: POST /api/scenario/start
  A->>E: await start_scenario(context)
  E->>E: require every assigned alias ready
  E-->>U: queue scenario_start for all participants
  E->>E: check local writes; 0.2 s then one retry stage
  U->>U: release immediately without a start ACK

  U->>W: video_end(final_frame_id)
  W->>E: end_video
  E->>E: close input, resolve gaps, drain work/results
  E-->>U: terminal video_status
  R->>A: poll /api/status until terminal accounting
  R->>A: POST /api/scenario/disarm
  A->>E: require fully idle, then clear authority
```

`client_seq` deduplicates `video_start` and `video_end` retries. Scenario
messages additionally carry `campaign_id`, `scenario_id`, and `attempt_id`.
A UE is usable for a release only after its current WebSocket generation has
acknowledged that exact attempt.

## Frame ingestion and terminal-result path

```mermaid
flowchart LR
  PKT["RTP packet"] --> PARSE["parse 96-byte descriptor<br/>before jitter buffer"]
  PARSE --> ASM["Assembly keyed by frame_id<br/>deduplicate payload ranges"]
  ASM --> DESC["descriptor event<br/>create Frame in RECEIVING"]
  ASM --> PROG["progress event<br/>unique bytes / completion"]
  PARSE --> JIT["GStreamer jitterbuffer"]
  JIT --> DEP["H.264 depay + parse + decode"]
  DEP --> RGB["RGB appsink callback"]

  DESC --> EQ["Engine event queue"]
  PROG --> EQ
  RGB --> EQ
  EQ --> RX["Engine.tick"]

  RX --> VALIDATE{"descriptor, video epoch,<br/>config and plan valid?"}
  VALIDATE -->|"no"| FAIL["failed terminal result"]
  VALIDATE -->|"yes"| READY{"decoded RGB available?"}
  READY -->|"no / expires or closes"| FAIL
  READY -->|"yes"| QUEUED["Frame state QUEUED<br/>application / priority deque"]
  QUEUED --> SUBMIT["Workers.submit<br/>reserve application stream"]
  SUBMIT --> PROCESS["child process preprocess,<br/>GPU inference, postprocess"]
  PROCESS --> OUT{"worker outcome"}
  OUT -->|"exception"| QUAR["quarantine candidate<br/>failed result"]
  OUT -->|"detection"| COMPLETE["completed result"]
  OUT -->|"segmentation RLE"| COMPLETE
  OUT -->|"tracking detections"| REORDER["REORDER_WAIT per video"]
  REORDER --> BYTE["serialized ByteTrack update<br/>age missing original frames"]
  BYTE --> COMPLETE

  FAIL --> FENCE["resolve once<br/>Ranges tombstone prevents duplicates"]
  QUAR --> FENCE
  COMPLETE --> FENCE
  FENCE --> JOBS["jobs.jsonl without payload"]
  FENCE --> DB["replay.sqlite3 with payload"]
  FENCE --> WSOUT["per-UE priority output queue"]
  DB -->|"after reconnect"| WSOUT
  WSOUT --> UE["UE frame_result"]
```

Collection mode does not discard work because an application deadline passed.
It still produces explicit failures for transport loss, incomplete decode,
worker exceptions, missing declared frames, queue/event overflow, and drain
watchdog failure. The scenario validator can therefore require complete
terminal accounting without claiming every frame completed inference.

## Files and responsibilities

| File | Primary responsibility |
|---|---|
| `app.py` | ASGI lifecycle, HTTP control/status, WebSocket connection queues and replay, UDP probe receiver |
| `config.py` | Pydantic configuration validation, catalog/profile loading, candidate eligibility |
| `engine.py` | Authoritative UE/video/frame state, scenario lifecycle, event draining, scheduling, EdgeRIC exchange, result delivery |
| `media.py` | Per-video GStreamer receiver, RTP metadata assembly, loss events and decoded RGB copying |
| `workers.py` | Spawned application workers, frozen TorchScript models, CUDA stream execution and result queues |
| `tracking.py` | One ordered ByteTrack instance per tracking video |
| `recording.py` | Asynchronous JSON evidence writer and durable SQLite result replay |
| `gpu.py` | Low-rate whole-device NVML telemetry without creating a CUDA context |
| `policies.py` | Adaptive GoP/candidate and priority heuristics; bypassed by fixed collection assignments where applicable |
| `statistics.py` | Processing histories and adaptive-mode network Kalman filter |

## Reading boundaries

- `Engine` is intentionally large because it is the single authority for the
  state machine. Treat its background tasks as concurrent producers, not as
  independent owners of frame state.
- The worker `ThreadPoolExecutor` permits one outstanding operation per
  configured CUDA priority stream. `Workers.busy` enforces that reservation in
  the parent process.
- `ReplayStore` is delivery durability, while `Recorder` is experiment
  evidence. They have separate paths and different payload retention.
- The adaptive coordination path (`choose`, `plan`, `coordinate`, suspension)
  remains in `engine.py`, but collection mode uses manifest-selected candidates,
  FIFO queues, and fixed priority.

