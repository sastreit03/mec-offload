#!/usr/bin/env bash
#
# check_ue_connection.sh
#
# Purpose: Script to be run on UE.
#          Gets and prints the UE's tunnel interface and IP address,
#          checks UE's routing table, and pings MEC server.
#          To save variables in parent shell process, run this scripts
#          as ". ./check_ue_connection.sh" or "source ./check_ue_connection.sh"
# Prerequisites:
# - UE docker container must be running.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[check-ue-connection] %s\n' "$*"
}

die() {
    printf '[check-ue-connection] ERROR: %s\n' "$*" >&2
    exit 1
}


# Parameters
UE_CONTAINER="${UE_CONTAINER:-oai-nr-ue}"
MEC_IP="${MEC_IP:-192.168.72.136}"


# Check that UE container is running
log "Checking that UE container is running..."
[[ "$(docker inspect -f '{{.State.Running}}' "$UE_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $UE_CONTAINER"


# Get tunnel interface name and IP address
UE_TUN=$(docker exec "$UE_CONTAINER" ip -o link show | awk -F': ' '$2 ~ /^oaitun_ue/ {print $2; exit}')
[[ -n "$UE_TUN" ]] || die "no oaitun_ue interface found in $UE_CONTAINER"

UE_IP=$(docker exec "$UE_CONTAINER" ip -4 -o addr show dev "$UE_TUN" | awk '{print $4}' | cut -d/ -f1 | head -n1)
[[ -n "$UE_IP" ]] || die "no IPv4 address found for $UE_TUN in $UE_CONTAINER"

# Print interface and IP address of UE
log "UE interface and IP address:"
log "UE_TUN=$UE_TUN"
log "UE_IP=$UE_IP"

# Get route decision on UE
log "Routing table from UE to MEC server:"
docker exec "$UE_CONTAINER" ip route get "$MEC_IP" from "$UE_IP" ||
    die "Unable to get routing table from container: $UE_CONTAINER"

# Ping MEC server from UE tunnel interface
log "Pinging MEC server from UE tunnel interface..."
docker exec "$UE_CONTAINER" ping -I "$UE_TUN" -c 5 "$MEC_IP" ||
    die "Unable to ping $MEC_IP from interface $UE_TUN in container $UE_CONTAINER"

log "Connection verified."
