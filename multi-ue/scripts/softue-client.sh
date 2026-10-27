#!/usr/bin/env bash
set -euo pipefail
# Assumes the new code and Python/GStreamer dependencies are mounted/installed in softUE.
container="${SOFTUE_CONTAINER:-oai-nr-ue}"
interface="${SOFTUE_INTERFACE:-oaitun_ue1}"
server="${MEC_SERVER:-192.168.72.136}"

# Aetheredge: route only MEC traffic through the cellular tunnel. The softUE
# retains eth0 for container management, while the tunnel address is discovered.
server_ip="$(docker exec "$container" python3 -c \
  'import socket,sys; print(socket.gethostbyname(sys.argv[1]))' "$server")"
source_ip="$(docker exec "$container" ip -4 -o addr show dev "$interface" | \
  awk '{sub(/\/.*/, "", $4); print $4; exit}')"
if [[ -z "$source_ip" ]]; then
  echo "No IPv4 address is assigned to softUE interface $interface" >&2
  exit 1
fi
docker exec "$container" ip route replace "$server_ip/32" \
  dev "$interface" src "$source_ip"

# Run inside the softUE namespace so control and media use the detected UE IP.
docker exec -e PYTHONPATH=/multi-ue/contracts/python:/multi-ue/ue/linux/src \
  "$container" python3 -m mec_ue.client --server "$server" \
  --source-interface "$interface" "$@"
