#!/usr/bin/env bash
set -euo pipefail

container="${SOFTUE_CONTAINER:-oai-nr-ue}"
interface="${SOFTUE_INTERFACE:-oaitun_ue1}"
server="${MEC_SERVER:-192.168.72.136}"
wait_s="${SOFTUE_ROUTE_WAIT_S:-60}"

# Aetheredge: discover the assigned tunnel address and route only MEC traffic
# through the cellular user plane, leaving eth0 available for management.
server_ip="$(docker exec "$container" python3 -c \
  'import socket,sys; print(socket.gethostbyname(sys.argv[1]))' "$server")"
deadline=$((SECONDS + wait_s))
source_ip=""
while [[ -z "$source_ip" && $SECONDS -lt $deadline ]]; do
  source_ip="$(docker exec "$container" ip -4 -o addr show dev "$interface" \
    2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}' || true)"
  [[ -n "$source_ip" ]] || sleep 1
done
if [[ -z "$source_ip" ]]; then
  echo "No IPv4 address was assigned to softUE interface $interface within ${wait_s}s" >&2
  exit 1
fi

docker exec "$container" ip route replace "$server_ip/32" \
  dev "$interface" src "$source_ip"
echo "Aetheredge MEC route: $server_ip dev $interface src $source_ip"
