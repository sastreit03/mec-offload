# EdgeRIC `muApp_mec` implementation diagrams

Audited 2026-11-02 from `EdgeRIC-v2/muApp_mec/`. This is the EdgeRIC path used
by the multi-UE collection system. It is separate from the historical Redis
muApps and from the optional FlexRIC nearRT-RIC monitor path.

## Runtime architecture

```mermaid
flowchart TB
  subgraph External["External systems"]
    OAI["OAI in-process EdgeRIC bridge<br/>protobuf Metrics"]
    MEC["MEC Engine<br/>Awareness client"]
  end

  subgraph Process["One muApp_mec asyncio process"]
    MAIN["__main__.py<br/>load YAML and construct Runtime"]

    subgraph Adapter["oai_adapter.py / OAIAdapter"]
      SUB["ZMQ SUB 127.0.0.1:5555<br/>CONFLATE newest sample"]
      DECODE["protobuf decode<br/>RNTI-keyed metrics and counters"]
      PUB["ZMQ PUB 127.0.0.1:5556<br/>UL weights"]
    end

    subgraph Runtime["runtime.py / Runtime"]
      GRPC["grpc.aio Awareness server :50052"]
      EX["Exchange unary handler<br/>sync + incremental awareness"]
      RS["Reports server stream<br/>latest UE-keyed report"]
      LOOP["cooperative run loop<br/>0.2-ms minimum sleep,<br/>radio, association, demand, windows"]
      RADIO["radio source generation,<br/>sequence fences and cumulative baselines"]
      WINDOW["telemetry.Window<br/>non-overlapping 100-ms deltas"]
      SESS["session registry<br/>video identity + expectation state"]
      ASSOC["Associations<br/>UE ID / connection to local RNTI leases"]
      LEDGER["one demand.Ledger per video<br/>predicted and actual outstanding bytes"]
      UTIL["heuristic.weights<br/>deadline / demand utility"]
      LATEST["latest_report<br/>RNTIs removed, UE IDs retained"]
      PROBECMD["queued probe_commands"]
    end

    subgraph Evidence["Local evidence"]
      LOGQ["bounded asyncio log queue"]
      WRITER["writer task + asyncio.to_thread"]
      EVENTS["events.jsonl"]
      SNAP["associations.json<br/>bindings and match scores"]
    end
  end

  OAI -->|"protobuf snapshots"| SUB --> DECODE --> LOOP
  LOOP --> RADIO --> WINDOW
  LOOP --> ASSOC
  LOOP --> SESS
  SESS --> LEDGER
  RADIO --> LEDGER
  ASSOC --> UTIL
  LEDGER --> UTIL
  UTIL -.->|"heuristic mode only"| PUB --> OAI

  MEC -->|"Awareness.Exchange"| GRPC --> EX
  EX --> ASSOC
  EX --> SESS
  EX --> LEDGER
  EX --> LOGQ
  WINDOW --> LATEST
  ASSOC --> LATEST
  LEDGER --> LATEST
  ASSOC --> PROBECMD --> LATEST
  LATEST --> RS -->|"Awareness.Reports"| MEC

  LOOP --> LOGQ
  EX --> LOGQ
  LOGQ --> WRITER --> EVENTS
  ASSOC --> SNAP
  MAIN --> LOOP
  MAIN --> GRPC
```

The OAI SUB socket is conflated, so EdgeRIC does not assume that it receives
every scheduler snapshot. Correct window totals come from differences between
cumulative counters published by OAI. The first sample of a radio generation
establishes a baseline and is not charged to the current window.

## MEC awareness and state reconciliation

```mermaid
sequenceDiagram
  autonumber
  participant M as MEC Engine
  participant G as gRPC Awareness server
  participant R as Runtime.exchange
  participant A as Associations
  participant S as Session registry
  participant L as Demand Ledger

  M->>G: Exchange(sync, awareness_generation, revision, context, associations, sessions)
  G->>R: exchange(message)
  R->>R: validate schema and complete attempt context
  alt new MEC awareness generation or run
    R->>R: retire old generation; clear sessions and bindings
  end
  alt new campaign/scenario/attempt context
    R->>R: clear radio baselines, window and latest report
  end
  R->>A: reconcile authoritative association snapshot
  loop each active session
    R->>S: merge UE/video/media epoch and control state
    S->>L: merge expectation, outstanding descriptors and progress
  end
  R-->>M: synced with EdgeRIC source_generation

  M->>G: Exchange(association/session/descriptor/progress/reception_loss/mec_resources)
  G->>R: exchange(incremental message)
  R->>R: reject stale revision, generation or scenario context
  alt association
    R->>A: register connection generation and IP
  else session or descriptor
    R->>S: validate media identity
    S->>L: update predicted or actual demand
  else progress
    R->>L: advance confirmed unique bytes; retire when complete
  else probe receipt
    R->>A: attach MEC receipt evidence to active probe
  end
  R-->>M: ok / stale / retired
```

The authoritative `sync` is the recovery mechanism for a dropped incremental
message or an MEC restart. `awareness_generation` separates MEC processes;
`awareness_revision` fences stale incremental updates; and the campaign,
scenario, and attempt IDs prevent evidence from being relabeled across runs.

## UE-to-RNTI association

```mermaid
sequenceDiagram
  autonumber
  participant M as MEC Engine
  participant R as EdgeRIC Runtime
  participant A as Associations
  participant U as One UE
  participant O as OAI radio metrics
  participant P as MEC UDP probe receiver

  M->>R: association(UE ID, connection ID, UE IP, generations)
  R->>A: register or invalidate binding
  A->>A: enqueue UE for serialized probing
  A-->>R: next_probe(pattern, start, interval, rate)
  R-->>M: Reports includes one probe command
  M-->>U: WebSocket association_probe
  U->>P: timed UDP bursts matching binary pattern
  P->>M: byte receipts per probe interval
  M->>R: Exchange(probe_receipt)
  O->>R: RNTI-keyed rx_bytes samples
  R->>A: sample metrics during each interval
  A->>A: correlate each RNTI series with pattern
  A->>A: require receipt, minimum correlation,<br/>margin, stable generation and no RNTI conflict
  alt accepted
    A->>A: bind opaque UE ID to local RNTI lease
  else rejected
    A->>A: record reason and requeue after backoff
  end
  R-->>M: UE-keyed report with association_valid
```

Only one signature probe is active at a time. This prevents simultaneous UEs
from producing ambiguous correlations, but makes initial four-UE association
inherently sequential. RNTIs and correlation scores remain in local EdgeRIC
evidence; reports sent to MEC are keyed by opaque UE ID.

## Radio counters, demand attribution and reports

```mermaid
flowchart LR
  Z["Newest conflated OAI snapshot"] --> FENCE{"new source generation<br/>or report sequence?"}
  FENCE -->|"new generation"| BASE["clear counter baselines and capacity cache;<br/>invalidate RNTI leases on source replacement"]
  FENCE --> CUR["read per-RNTI cumulative<br/>UL/DL new and retransmission PRB/TBS"]
  BASE --> CUR
  CUR --> DELTA["subtract previous counters"]
  DELTA -->|"negative delta"| BAD["coverage invalid for window"]
  DELTA -->|"valid delta"| WIN["Window.add_counters"]
  DELTA -->|"UL new-data TBS only"| SERVICE["provisional FIFO service attribution"]

  MECMSG["MEC expectation + actual descriptor<br/>+ unique-byte progress"] --> LEDGER["per-video Ledger"]
  SERVICE --> LEDGER
  LEDGER --> SNAP["remaining bytes, oldest frame,<br/>earliest deadline, overdue bytes"]
  SNAP --> WEIGHT["heuristic.weights"]
  CAP["fresh OAI reference TBS<br/>capacity bytes per PRB"] --> WEIGHT
  WEIGHT --> MODE{"runtime mode"}
  MODE -->|"shadow"| LOG["log proposed weights only"]
  MODE -->|"heuristic"| WIRE["publish RNTI weights on ZMQ 5556"]

  WIN --> CLOSE["close each 100-ms wall-clock window"]
  CLOSE --> MAP["map valid RNTI leases to UE IDs"]
  MAP --> REPORT["latest report:<br/>UL/DL PRB and TBS totals,<br/>MCS, BLER, SNR, RLC,<br/>capacity, demand snapshot"]
  BAD --> REPORT
  REPORT --> STREAM["Awareness.Reports stream to MEC"]
  REPORT --> RAW["events.jsonl radio_report<br/>retains local RNTI detail separately"]
```

Confirmed MEC unique-byte progress and provisionally estimated radio service
are never added together. `FrameDemand.remaining` subtracts the larger of the
two, limiting double credit. Scheduled TBS remains telemetry; it does not
become proof that application bytes arrived.

## Files and responsibilities

| File | Primary responsibility |
|---|---|
| `__main__.py` | YAML loading, OAI adapter construction and asyncio runtime entry point |
| `runtime.py` | gRPC service, awareness reconciliation, main radio loop, demand prediction, window publication and optional actions |
| `oai_adapter.py` | Conflated protobuf ZMQ input and scheduling-weight ZMQ output |
| `association.py` | Serialized probe generation, correlation, lease/revalidation and conflict checks |
| `demand.py` | Per-video frame-demand ledger, actual/provisional byte accounting and compact retirement tombstones |
| `telemetry.py` | 100-ms counter aggregation; also contains the older offline CSV trace reader |
| `heuristic.py` | Demand/deadline utility transformed into normalized UE weights |
| `legacy_wire/*.proto` | Protobuf schema used on the local OAI ZMQ boundary |

## Active-campaign interpretation

- The current collection campaign uses `shadow` mode. The heuristic is
  evaluated and may be logged, but `OAIAdapter.send()` is not called.
- OAI therefore continues with its configured PF scheduler. EdgeRIC is an
  observer and association/telemetry service for this campaign.
- `Window` values are 100-ms aggregates derived from cumulative scheduler
  counters. They are neither per-slot logs nor per-frame measurements.
- `throughput_bytes_s` is a payload-fraction conversion from scheduled
  new-data TBS. It is distinct from MEC received unique bytes and UE-observed
  application results.
- A report before a valid association has unavailable UE attribution. Null
  radio state and zero attributable grants in that row must not be interpreted
  as a measured zero-quality radio link.

