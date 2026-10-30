#!/usr/bin/env bash
# Best-effort Aetheredge control-plane recovery. The runner retries the exact
# scenario with a new attempt ID after this hook; it never changes the manifest.
set -u

repo_root="${AETHEREDGE_REPO_ROOT:-$HOME/mec-offload-ad}"
mec_host="${AETHEREDGE_MEC_HOST:-sstreit@192.168.71.130}"

reason="${1:-unknown}"
scenario_id="${2:-unknown-scenario}"
attempt_id="${3:-unknown-attempt}"

# Preserve RAN evidence before Compose can restart or recreate the gNB.
recovery_dir="$repo_root/multi-ue/runs/campaigns/aetheredge-fixed-policy-v1/recovery/$scenario_id/$attempt_id"
mkdir -p "$recovery_dir"
docker inspect oai-gnb >"$recovery_dir/oai-gnb-inspect.json" 2>&1 || true
docker logs --since 15m oai-gnb >"$recovery_dir/oai-gnb.log" 2>&1 || true

gnb_running="$(docker inspect -f '{{.State.Running}}' oai-gnb 2>/dev/null || true)"
if [[ "$gnb_running" != true \
  || "$reason" == *"gNB channel controller"* \
  || "$reason" == *"assignment acknowledgement"* \
  || "$reason" == *"UE disconnected during scenario"* \
  || "$reason" == *"scenario stalled"* ]]; then
  # The CIR loader resets to pass-through. The runner selects and verifies the
  # manifest trace again before it arms the retry.
  (
    cd "$repo_root/gnb-core/config/common" || exit 1
    if [[ "$gnb_running" == true ]]; then
      docker restart oai-gnb
    else
      USER_ID="$(id -u)" GROUP_ID="$(id -g)" \
        docker compose --env-file ../b200/.env up -d oai-gnb
    fi
  ) || true

  # Do not let a broken RAN recovery block the campaign indefinitely.
  for _ in $(seq 1 90); do
    health="$(docker inspect -f '{{.State.Health.Status}}' oai-gnb 2>/dev/null || true)"
    [[ "$health" == healthy ]] && break
    sleep 1
  done
fi

if ! docker inspect -f '{{.State.Running}}' edgeric-mec 2>/dev/null | grep -qx true \
  || [[ "$reason" == *"EdgeRIC"* ]]; then
  "$repo_root/multi-ue/scripts/containers.sh" stop-edgeric || true
  "$repo_root/multi-ue/scripts/containers.sh" start-edgeric || true
fi

restart_mec=false
if ! timeout 5 curl -fsS http://192.168.71.142:8765/readyz >/dev/null 2>&1; then
  restart_mec=true
elif [[ "$reason" == *"scenario timed out"* \
  || "$reason" == *"scenario stalled"* \
  || "$reason" == *"unresolved frames"* \
  || "$reason" == *"terminal frame"* \
  || "$reason" == *"final frame"* \
  || "$reason" == *"candidate mismatch"* \
  || "$reason" == *"scenario context"* \
  || "$reason" == *"scenario arm"* \
  || "$reason" == *"assignment acknowledgement"* \
  || "$reason" == *"runner stopped"* \
  || "$reason" == *"runner completed"* \
  || "$reason" == *"MEC must be idle"* \
  || "$reason" == *"collection"* ]]; then
  # A failed active attempt may not be idle enough to arm its retry.
  restart_mec=true
fi

if $restart_mec; then
  # Batch mode prevents a campaign from blocking on an interactive password.
  timeout 90 ssh -o BatchMode=yes -o ConnectTimeout=5 "$mec_host" \
    "cd ~/mec-offload-ad/multi-ue && ./scripts/containers.sh stop-mec || true; ./scripts/containers.sh start-mec" \
    || true
fi

exit 0
