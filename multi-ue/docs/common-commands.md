# Aetheredge common commands

This is the short operational command reference for the Aetheredge testbed.
Add commands here as they become part of the regular workflow.

The complete per-scenario checklist and mistake-recovery sequence are in
[`campaign-runbook.md`](campaign-runbook.md).

## Check whether the MEC is ready

Run on the **MEC Spark** from any directory:

```bash
docker exec multi-ue-mec python3 -c '
import json
import urllib.request

result = json.load(
    urllib.request.urlopen(
        "http://127.0.0.1:8765/readyz",
        timeout=5,
    )
)
print(json.dumps(result, indent=2))
'
```

The MEC is ready when the output contains:

```text
"ready": true
```

For campaign collection, it should also contain:

```text
"mode": "collection"
```

If the container may have exited, check its state and recent logs:

```bash
docker inspect multi-ue-mec \
  --format='status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{json .State.Error}}'

docker logs --tail 200 multi-ue-mec
```

## Check UE connection and EdgeRIC association

Run on the **MEC Spark** from any directory while `multi-ue-mec` is running:

```bash
docker exec -i multi-ue-mec python3 - <<'PY'
import json
import urllib.request

status = json.load(
    urllib.request.urlopen(
        "http://127.0.0.1:8765/api/status",
        timeout=5,
    )
)

print("mode:", status["readiness"].get("mode"))
print("radio_report_age_s:", status["radio_report_age_s"])
print("unresolved:", status["unresolved"])
print("UEs:")

for ue_id, ue in status["ues"].items():
    print(
        " ",
        ue_id,
        "alias=" + str(ue.get("alias")),
        "connected=" + str(ue.get("connected")),
        "association_valid=" + str(ue.get("association_valid")),
        "scenario_acknowledged=" + str(ue.get("scenario_acknowledged")),
        "ip=" + str(ue.get("ip")),
    )
PY
```

Before starting a scenario, every assigned UE should report:

```text
connected=True association_valid=True
```

After the runner arms a scenario, every assigned UE must also report
`scenario_acknowledged=True`. A missing acknowledgement means the assignment
did not complete a bidirectional control round trip; the runner invalidates
the attempt and invokes recovery instead of collecting a partial scenario.
Once every acknowledgement is present, `scenario_start_unix_ns` in `/api/status`
shows the shared start barrier released by the runner.

Collection preflight also expects:

```text
mode: collection
unresolved: 0
radio_report_age_s: less than 1 second
```

If a UE remains invalid or alternates between valid and invalid, inspect the
latest probe result on the **gNB Spark**:

```bash
cd ~/mec-offload-ad/multi-ue

python3 - <<'PY'
import json
from pathlib import Path

paths = list(Path("runs/edgeric").glob("*/associations.json"))
if not paths:
    raise SystemExit("no EdgeRIC association snapshot found")

path = max(paths, key=lambda value: value.stat().st_mtime_ns)
data = json.loads(path.read_text())
print("snapshot:", path)
print(json.dumps(data, indent=2, sort_keys=True))
PY
```

The probe result reports `received_bytes`, `best_rnti`, `best_correlation`,
`margin`, and a specific failure `reason`. This distinguishes missing UE probe
traffic from incomplete RAN samples and an ambiguous RNTI match.

## Start the Linux campaign UE client

Run on the **Orin** after the softUE has attached. Leave this command running
for the campaign; the runner supplies the application, model, bitrate, media,
and source-frame range for each active scenario.

```bash
cd ~/mec-offload-ad/multi-ue

./scripts/softue-client.sh \
  --source-dir /datasets/campaign \
  --ue-alias linux1 \
  --application detection \
  --logs /multi-ue/runs/campaign-linux1-51prb
```

The initial `detection` value is only a placeholder before the first campaign
assignment. Confirm `linux1` reports `connected=True` and
`association_valid=True` with the association check above.

## Check whether scenario media has started or stalled

Run on the **MEC Spark**. This is a one-shot check and does not wait for a
particular state:

```bash
docker exec -i multi-ue-mec python3 - <<'PY'
import json
import urllib.request

status = json.load(
    urllib.request.urlopen(
        "http://127.0.0.1:8765/api/status",
        timeout=5,
    )
)

print("scenario:", json.dumps(status.get("scenario"), sort_keys=True))
print("unresolved:", status.get("unresolved"))
print("videos:")

for video_id, video in status.get("videos", {}).items():
    print(
        " ",
        video_id,
        "ue_alias=" + str(video.get("ue_alias")),
        "application=" + str(video.get("application")),
        "state=" + str(video.get("state")),
        "received_frames=" + str(video.get("received_frames")),
        "pipeline_states=" + json.dumps(video.get("pipeline_states"), sort_keys=True),
        "counters=" + json.dumps(video.get("counters"), sort_keys=True),
        "final_frame_id=" + str(video.get("final_frame_id")),
        "collection_error=" + str(video.get("collection_error")),
    )
PY
```

An `ACTIVE` video with nonzero counters confirms that media has started. A
finished 150-frame scenario should have a terminal state, `final_frame_id=149`,
150 total counter outcomes, `unresolved=0`, and no `collection_error`.

## Check campaign runner state

Run on the **gNB Spark** from `~/mec-offload-ad/multi-ue`:

```bash
python3 - <<'PY'
import sqlite3

db = sqlite3.connect(
    "runs/campaigns/aetheredge-fixed-policy-v1/state.sqlite3"
)

for row in db.execute("""
    SELECT scenario_id, status, attempts, attempt_id, error
    FROM scenarios
    WHERE status != 'pending'
    ORDER BY started_unix_ns DESC
    LIMIT 10
"""):
    print(row)
PY
```

To retry one scenario that reached `failed` after its cause has been repaired,
run on the **gNB Spark**:

```bash
cd ~/mec-offload-ad/multi-ue

SCENARIO_ID="scn-replace-me"

../gnb-core/env/bin/python tools/run_campaign.py \
  --campaign-dir runs/campaigns/aetheredge-fixed-policy-v1 \
  --trace-bank ../gnb-core/plugins/channel_emulation/data/trace_bank \
  --scenario-id "$SCENARIO_ID" \
  --retry-failed
```

`--retry-failed` only reopens a selected checkpoint whose status is `failed`.
It does not repeat a scenario already marked `valid`.

The collection MEC allows 30 seconds for a control reconnect; the runner's
default disconnect grace is ten seconds. Keep the MEC value larger so the
runner, rather than the MEC lifecycle timer, decides whether to retry.

## Follow runner monitoring events

Run on the **gNB Spark** from `~/mec-offload-ad/multi-ue`:

```bash
tail -f runs/campaigns/aetheredge-fixed-policy-v1/runner-events.jsonl
```

## Generate and run the 51-PRB no-emulator campaign

Run on the **gNB Spark** from `~/mec-offload-ad/multi-ue`:

```bash
CAMPAIGN=aetheredge-paired-per-ue-51prb-no-emulator-v1

python3 tools/generate_campaign_manifest.py \
  --campaign "config/campaigns/${CAMPAIGN}.yaml" \
  --catalog config/tasks.yaml \
  --media-inventory config/campaigns/media-inventory.yaml \
  --output "runs/campaigns/${CAMPAIGN}"

python3 tools/run_campaign.py \
  --campaign-dir "runs/campaigns/${CAMPAIGN}" \
  --disconnect-grace 10
```

This campaign has 780 scenarios: 12 one-UE and 768 four-UE scenarios. Each UE
independently selects one of four paired DNN/bitrate tiers. The runner does not
contact the CIR controller and does not require `--trace-bank`.

Confirm that the running gNB has no channel-emulator listener or options:

```bash
docker exec oai-gnb sh -c \
  'ss -ltn 2>/dev/null | grep ":5560 " || true'

docker logs oai-gnb 2>&1 | grep -Ei \
  'Channel emulation enabled|CIR ZMQ initialized|--cir-(folder|zmq)' || true
```

## Preserve Pixel application evidence

Run on the **MacBook** connected to the Pixel over USB:

```bash
adb exec-out run-as edu.clemson.mecrtp \
  cat /storage/emulated/0/Android/data/edu.clemson.mecrtp/files/events.jsonl \
  > pixel1-events.jsonl

grep '"scenario_id":"scn-' pixel1-events.jsonl | tail -50
```

## Export the joined campaign dataset

Run on the **MEC Spark** after EdgeRIC and UE evidence has been copied under
`runs/export-input`. The exporter includes only each scenario's latest attempt
marked `valid`.

```bash
cd ~/mec-offload-ad/multi-ue

python3 tools/export_campaign_dataset.py \
  --campaign-dir runs/campaigns/aetheredge-fixed-policy-v1 \
  --mec-root runs \
  --edgeric-root runs/export-input/edgeric \
  --ue-log pixel1=runs/export-input/ue/pixel1-events.jsonl \
  --ue-log pixel2=runs/export-input/ue/pixel2-events.jsonl \
  --ue-log pixel3=runs/export-input/ue/pixel3-events.jsonl \
  --ue-log linux1=runs/export-input/ue/linux1-events.jsonl \
  --output runs/campaigns/aetheredge-fixed-policy-v1/dataset \
  --strict

python3 -m json.tool \
  runs/campaigns/aetheredge-fixed-policy-v1/dataset/validation.json
```

