#!/usr/bin/env bash
set -euo pipefail
# Assumes the new code and Python/GStreamer dependencies are mounted/installed in softUE.
container="${SOFTUE_CONTAINER:-oai-nr-ue}"
interface="${SOFTUE_INTERFACE:-oaitun_ue1}"
# Run inside the softUE namespace so interface discovery and traffic use its UE address.
docker exec -e PYTHONPATH=/multi-ue/contracts/python:/multi-ue/ue/linux/src "$container" python3 -m mec_ue.client --server "${MEC_SERVER:-192.168.72.136}" --source-interface "$interface" "$@"
