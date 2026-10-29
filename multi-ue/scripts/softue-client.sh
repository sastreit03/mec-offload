#!/usr/bin/env bash
set -euo pipefail
# Assumes the new code and Python/GStreamer dependencies are mounted/installed in softUE.
container="${SOFTUE_CONTAINER:-oai-nr-ue}"
interface="${SOFTUE_INTERFACE:-oaitun_ue1}"
server="${MEC_SERVER:-192.168.72.136}"

# Reapply the idempotent route in case the tunnel was recreated after startup.
"$(dirname "${BASH_SOURCE[0]}")/softue-route.sh"

# Run inside the softUE namespace so control and media use the detected UE IP.
docker exec -e PYTHONPATH=/multi-ue/contracts/python:/multi-ue/ue/linux/src \
  "$container" python3 -m mec_ue.client --server "$server" \
  --source-interface "$interface" "$@"
