# Aetheredge campaign run checklist

Use this checklist for each one-UE pilot or campaign scenario. Commands marked
**MEC Spark** run on the Spark hosting the core and MEC. Commands marked **gNB
Spark** run on the Spark hosting the gNB, EdgeRIC, channel controller, and
campaign runner.

The runner checkpoint is
`runs/campaigns/aetheredge-fixed-policy-v1/state.sqlite3`. Do not delete this
database or regenerate the manifest during a campaign. The runner skips every
scenario already marked `valid`.

## Normal sequence for one scenario

### 1. Check the MEC service

On the **MEC Spark**:

```bash
cd ~/mec-offload-ad/multi-ue

docker inspect multi-ue-mec \
  --format='status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'

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

Continue only when the container reports `status=running`, readiness reports
`"ready": true`, and the readiness mode is `collection`.

If the MEC is stopped, start it with the collection configuration:

```bash
cd ~/mec-offload-ad/multi-ue
./scripts/containers.sh start-mec
```

### 2. Check the gNB, EdgeRIC, and channel controller

On the **gNB Spark**:

```bash
cd ~/mec-offload-ad

docker inspect oai-gnb edgeric-mec \
  --format='{{.Name}} status={{.State.Status}}'

gnb-core/env/bin/python multi-ue/tools/channel_control.py status
```

Both containers must be running. The channel status may show the previous
condition; the runner selects the scenario's condition before arming the MEC.

### 3. Prepare the Pixel before starting the runner

On **Pixel 1** for a one-UE scenario:

1. Open the Aetheredge application.
2. Confirm the MEC server is `192.168.72.136`.
3. Confirm the UE alias is `pixel1`.
4. Press **Choose dataset video directory** and select the campaign directory.
5. Confirm the application reports `Dataset directory: 42 videos`.
6. Press **Connect and stream** once.
7. Wait for `MEC control connected`.

The campaign assignment controls the application, model, bitrate, media file,
and source-frame range. The application spinner changes to the assigned
application after the runner arms the scenario.

The dataset directory must be selected again after reinstalling the app,
clearing its data, or reopening it when the current process has lost its
playlist. Seeing `42 videos` is the required check.

### 4. Check the UE association

On the **MEC Spark**:

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

for ue_id, ue in status["ues"].items():
    print(
        ue_id,
        "alias=" + str(ue.get("alias")),
        "connected=" + str(ue.get("connected")),
        "association_valid=" + str(ue.get("association_valid")),
        "ip=" + str(ue.get("ip")),
    )
PY
```

Continue only when `pixel1` reports both `connected=True` and
`association_valid=True`. Also require `unresolved: 0` and a
`radio_report_age_s` below one second.

### 5. Select one pending one-UE scenario

On the **gNB Spark**:

```bash
cd ~/mec-offload-ad/multi-ue

SCENARIO_ID="$(
python3 - <<'PY'
import json
import sqlite3

campaign = "runs/campaigns/aetheredge-fixed-policy-v1"
db = sqlite3.connect(f"{campaign}/state.sqlite3")
states = dict(db.execute("SELECT scenario_id, status FROM scenarios"))

with open(f"{campaign}/scenarios.jsonl") as stream:
    for line in stream:
        scenario = json.loads(line)
        if (
            scenario["factors"]["ue_set"] == "one_ue"
            and states.get(scenario["scenario_id"], "pending") == "pending"
        ):
            print(scenario["scenario_id"])
            break
PY
)"

test -n "$SCENARIO_ID" || {
  echo "No pending one-UE scenario is available" >&2
  false
}

echo "SCENARIO_ID=$SCENARIO_ID"
```

Display its assignment before running it:

```bash
python3 - "$SCENARIO_ID" <<'PY'
import json
import sys

scenario_id = sys.argv[1]
path = "runs/campaigns/aetheredge-fixed-policy-v1/scenarios.jsonl"

with open(path) as stream:
    scenario = next(
        json.loads(line)
        for line in stream
        if json.loads(line)["scenario_id"] == scenario_id
    )

print("scenario:", scenario["scenario_id"])
print("factors:", json.dumps(scenario["factors"], sort_keys=True))
print("channel:", scenario["channel"]["condition_id"])
for assignment in scenario["ues"]:
    print("assignment:", json.dumps(assignment, sort_keys=True))
PY
```

### 6. Start the runner

Keep the Pixel application open with **Connect and stream** already enabled.
Then run on the **gNB Spark**:

```bash
../gnb-core/env/bin/python tools/run_campaign.py \
  --campaign-dir runs/campaigns/aetheredge-fixed-policy-v1 \
  --trace-bank ../gnb-core/plugins/channel_emulation/data/trace_bank \
  --scenario-id "$SCENARIO_ID"
```

Do not press **Connect and stream** again while this command is running.

If media, terminal counters, or MEC drain state make no progress for 30
seconds, the runner marks the attempt retryable and invokes automatic recovery.
The separate 180-second scenario timeout remains the total upper bound.

### 7. Monitor the run

In another **gNB Spark** terminal:

```bash
cd ~/mec-offload-ad/multi-ue
tail -f runs/campaigns/aetheredge-fixed-policy-v1/runner-events.jsonl
```

On the **MEC Spark**, use this nonblocking status check:

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
for video_id, video in status.get("videos", {}).items():
    print(
        video_id,
        "state=" + str(video.get("state")),
        "received_frames=" + str(video.get("received_frames")),
        "pipeline_states=" + json.dumps(video.get("pipeline_states"), sort_keys=True),
        "counters=" + json.dumps(video.get("counters"), sort_keys=True),
        "final_frame_id=" + str(video.get("final_frame_id")),
        "collection_error=" + str(video.get("collection_error")),
    )
PY
```

An active video with nonzero counters confirms media flow. A valid 150-frame
run finishes with all of the following:

```text
state=COMPLETED
final_frame_id=149
completed + dropped + failed = 150
unresolved=0
collection_error=None
```

### 8. Confirm the checkpoint

After the runner returns, run on the **gNB Spark**:

```bash
python3 - "$SCENARIO_ID" <<'PY'
import sqlite3
import sys

db = sqlite3.connect(
    "runs/campaigns/aetheredge-fixed-policy-v1/state.sqlite3"
)
print(db.execute("""
    SELECT scenario_id, status, attempts, attempt_id, error
    FROM scenarios
    WHERE scenario_id = ?
""", (sys.argv[1],)).fetchone())
PY
```

The required final status is `valid`.

## Restart after an operator mistake or interrupted run

Use this procedure if the wrong directory or alias was selected, the Pixel was
not connected, the runner was interrupted, or the attempt stalled.

Before closing a stalled Pixel application, preserve its local evidence from
the **MacBook** connected over USB:

```bash
adb exec-out run-as edu.clemson.mecrtp \
  cat /storage/emulated/0/Android/data/edu.clemson.mecrtp/files/events.jsonl \
  > pixel1-events.jsonl

grep '"scenario_id":"scn-' pixel1-events.jsonl | tail -50
```

For a known scenario, replace the broad `grep` with its exact scenario ID. A
`sender_error`, `media_setup_error`, or `control_reconnect` near the last frame
identifies which Pixel path stopped.

### 1. Stop the runner

In its **gNB Spark** terminal, press `Ctrl-C` once. Confirm that it exited:

```bash
pgrep -af 'tools/run_campaign.py' || echo "Runner is not running"
```

The runner records the active attempt as `interrupted`. Do not delete the
checkpoint database.

### 2. Clear the incomplete MEC attempt

On the **MEC Spark**, leave MPS running and restart only the MEC:

```bash
cd ~/mec-offload-ad/multi-ue
./scripts/containers.sh stop-mec
./scripts/containers.sh start-mec
```

Run the readiness check from step 1 and wait for `"ready": true` in collection
mode.

### 3. Reset the Pixel application

1. Close the Aetheredge application completely and reopen it.
2. Confirm MEC server `192.168.72.136` and the correct alias.
3. Select the campaign dataset directory again.
4. Confirm `Dataset directory: 42 videos`.
5. Press **Connect and stream** once.
6. Wait for `MEC control connected`.
7. Repeat the association check and require `association_valid=True`.

### 4. Recover the interrupted scenario ID

If the shell still has `$SCENARIO_ID`, keep it. Otherwise, recover the latest
nonvalid attempted scenario on the **gNB Spark**:

```bash
cd ~/mec-offload-ad/multi-ue

read -r SCENARIO_ID ATTEMPTS <<EOF
$(python3 - <<'PY'
import sqlite3

db = sqlite3.connect(
    "runs/campaigns/aetheredge-fixed-policy-v1/state.sqlite3"
)
row = db.execute("""
    SELECT scenario_id, attempts
    FROM scenarios
    WHERE status IN ('running', 'interrupted', 'retryable', 'failed')
    ORDER BY started_unix_ns DESC
    LIMIT 1
""").fetchone()
if row:
    print(*row)
PY
)
EOF

test -n "$SCENARIO_ID" || {
  echo "No interrupted or failed scenario was found" >&2
  false
}

echo "SCENARIO_ID=$SCENARIO_ID attempts=$ATTEMPTS"
```

### 5. Resume without editing the checkpoint

The runner's attempt limit is cumulative. Set the new limit above the number
already used, then run the same scenario:

```bash
MAX_ATTEMPTS=$((ATTEMPTS + 3))

../gnb-core/env/bin/python tools/run_campaign.py \
  --campaign-dir runs/campaigns/aetheredge-fixed-policy-v1 \
  --trace-bank ../gnb-core/plugins/channel_emulation/data/trace_bank \
  --scenario-id "$SCENARIO_ID" \
  --max-attempts "$MAX_ATTEMPTS"
```

This creates a new `attempt_id` for the same immutable `scenario_id`. It does
not regenerate the scenario and does not rerun scenarios already marked
`valid`.

If the mistake does not need the same scenario for testing, select a fresh
pending scenario using normal step 5 instead.

## Validate Pixel control reconnection

Perform this test while no scenario is running and the Pixel still has
**Connect and stream** enabled.

1. On the **MEC Spark**, restart only the MEC. Leave MPS, the core, gNB, and
   EdgeRIC running:

   ```bash
   cd ~/mec-offload-ad/multi-ue
   ./scripts/containers.sh stop-mec
   ./scripts/containers.sh start-mec
   ```

2. Wait for MEC readiness using normal step 1.
3. Do not press **Connect and stream** again. The Pixel should report
   `MEC control connected` automatically.
4. Run the association check from normal step 4 until `pixel1` again reports
   `connected=True` and `association_valid=True`.
5. Run one fresh pending scenario using normal steps 5 through 8. Require 150
   terminal frame outcomes and a `valid` checkpoint.

This validates control reconnection and re-association after MEC replacement.
It does not test runner checkpoint recovery.

## Validate runner interruption and resume

1. Select a fresh pending scenario and start it using normal steps 5 and 6.
2. After the Pixel frame box shows an increasing `transmitted` frame, or the
   MEC status shows increasing `received_frames`, press `Ctrl-C` once in the
   runner terminal. Do not require `completed` to increase: ordered tracking
   can hold detector results behind an incomplete earlier frame until input
   closes.
3. Confirm the runner exited. Do not restart the MEC or close the Pixel.
4. Allow the already-armed Pixel session to transmit through frame 149. Then
   use the MEC status check until the video is terminal and `unresolved=0`.
   Do not restart the runner while the old video is still `ACTIVE` or draining.
5. Run the same runner command again with the same `$SCENARIO_ID`.
6. The runner creates a new attempt for that scenario. Confirm the second
   attempt finishes and the checkpoint becomes `valid`.

The first interruption consumes one attempt. The default limit of three is
enough for this single interruption. If prior failures have already exhausted
the limit, use the higher cumulative `--max-attempts` procedure in the recovery
section.

# Campaign retry behavior

The runner allows a connected UE ten seconds to restore its control WebSocket
before declaring the attempt failed. This avoids discarding a complete radio
and compute record because of one missed one-second status poll. A longer
disconnect remains an attempt failure.

Before retrying any failed attempt, the runner asks the MEC to disarm it. A
drained attempt is cleared without restarting resident workers. If active
video or delivery state prevents disarming, the HTTP 409 response body is
preserved and the recovery hook restarts the MEC before the next arm request.
This prevents repeated `HTTP Error 409: Conflict` failures against stale MEC
scenario state.

After repairing a runner or system fault, a scenario whose checkpoint already
reached `failed` can be deliberately reopened with `--retry-failed` together
with `--scenario-id`. Valid scenarios are never reset or regenerated.

Collection clients retain `video_end` until the MEC acknowledges its original
`client_seq`, and resend it after a control reconnect. The collection MEC keeps
an active media lifecycle for 30 seconds after a control disconnect, longer
than the runner's default ten-second reconnect grace. This lets transient radio
re-establishment finish the same attempt while preserving strict 150-outcome
validation.

# 51-PRB physical-RF campaign

`aetheredge-paired-per-ue-51prb-no-emulator-v1` disables CIR emulation and
crosses the four paired DNN/bitrate tiers independently across active UEs. It
contains 12 one-UE scenarios and 768 four-UE scenarios. Generate and run it
without a trace-bank argument:

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

Each scenario records `channel.condition_id=physical-rf` and
`channel.emulation_enabled=false`. EdgeRIC measurements describe observed
radio conditions; they are not labels for a controlled synthetic channel.

