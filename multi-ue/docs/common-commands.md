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
        "ip=" + str(ue.get("ip")),
    )
PY
```

Before starting a scenario, every assigned UE should report:

```text
connected=True association_valid=True
```

Collection preflight also expects:

```text
mode: collection
unresolved: 0
radio_report_age_s: less than 1 second
```

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

## Follow runner monitoring events

Run on the **gNB Spark** from `~/mec-offload-ad/multi-ue`:

```bash
tail -f runs/campaigns/aetheredge-fixed-policy-v1/runner-events.jsonl
```

## Preserve Pixel application evidence

Run on the **MacBook** connected to the Pixel over USB:

```bash
adb exec-out run-as edu.clemson.mecrtp \
  cat /storage/emulated/0/Android/data/edu.clemson.mecrtp/files/events.jsonl \
  > pixel1-events.jsonl

grep '"scenario_id":"scn-' pixel1-events.jsonl | tail -50
```

