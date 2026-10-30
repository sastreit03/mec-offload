#!/usr/bin/env bash
# Best-effort Aetheredge control-plane recovery. The runner retries the exact
# scenario with a new attempt ID after this hook; it never changes the manifest.
set -u

repo_root="${AETHEREDGE_REPO_ROOT:-$HOME/mec-offload-ad}"
mec_host="${AETHEREDGE_MEC_HOST:-sstreit@192.168.71.130}"

reason="${1:-unknown}"
if ! docker inspect -f '{{.State.Running}}' edgeric-mec 2>/dev/null | grep -qx true \
  || [[ "$reason" == *"EdgeRIC"* ]]; then
  "$repo_root/multi-ue/scripts/containers.sh" stop-edgeric || true
  "$repo_root/multi-ue/scripts/containers.sh" start-edgeric || true
fi

restart_mec=false
if ! timeout 5 curl -fsS http://192.168.71.142:8765/readyz >/dev/null 2>&1; then
  restart_mec=true
elif [[ "$reason" == *"scenario timed out"* \
  || "$reason" == *"unresolved frames"* \
  || "$reason" == *"terminal frame"* \
  || "$reason" == *"final frame"* \
  || "$reason" == *"candidate mismatch"* \
  || "$reason" == *"scenario context"* \
  || "$reason" == *"scenario arm"* \
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
