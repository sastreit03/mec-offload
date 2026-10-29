#!/usr/bin/env bash
set -euo pipefail

container="${SOFTUE_CONTAINER:-oai-nr-ue}"
interface="${SOFTUE_INTERFACE:-oaitun_ue1}"
server="${MEC_SERVER:-192.168.72.136}"
data_network="${SOFTUE_DATA_NETWORK:-192.168.72.128/26}"
traffic_server="${SOFTUE_TRAFFIC_SERVER:-192.168.72.135}"
wait_s="${SOFTUE_ROUTE_WAIT_S:-60}"

# Aetheredge: discover the assigned tunnel address before adding cellular
# data-network routes, leaving eth0 available for management traffic.
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
# Aetheredge: keep all N6 services, including the traffic generator used for
# validation, on the cellular data path after every softUE container launch.
docker exec "$container" ip route replace "$data_network" \
  dev "$interface" src "$source_ip"

# Fail startup instead of silently sending application traffic over eth0.
for destination in "$traffic_server" "$server_ip"; do
  route="$(docker exec "$container" ip route get "$destination")"
  if [[ "$route" != *"dev $interface"* || "$route" != *"src $source_ip"* ]]; then
    echo "Incorrect softUE route for $destination: $route" >&2
    exit 1
  fi
done
echo "Aetheredge cellular routes: $server_ip/32 and $data_network dev $interface src $source_ip"
