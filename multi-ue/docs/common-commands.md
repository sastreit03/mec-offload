# Aetheredge common commands

This is the short operational command reference for the Aetheredge testbed.
Add commands here as they become part of the regular workflow.

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

