#!/bin/bash
#
# check_ue_connection.sh
#
# Purpose: Script to be run on UE.
#          Gets and prints the UE's tunnel interface and IP address,
#          checks UE's routing table, and pings MEC server.
#          To save variables in parent shell process, run this scripts
#          as ". ./get_tun_ip.sh" or "source ./get_tun_ip.sh"
# Prerequisites:
# - UE docker container must be running.
#
# Acknowledgement: Commands below were written by Generative AI.

# Parameters
UE_IMAGE="${UE_IMAGE:-oai-nr-ue-cuda:latest}"
UE_CONTAINER=oai-nr-ue
MEC_IP=192.168.72.136

# Check that the UE docker container is running
[[ -n "$(docker ps -q --filter "ancestor=$UE_IMAGE")" ]] \
  || { echo "ERROR: no active docker container found for image $UE_IMAGE" >&2; exit 1; }

# Get tunnel interface name
UE_TUN=$(docker exec "$UE_CONTAINER" ip -o link show | awk -F': ' '$2 ~ /^oaitun_ue/ {print $2; exit}')
UE_IP=$(docker exec "$UE_CONTAINER" ip -4 -o addr show dev "$UE_TUN" | awk '{print $4}' | cut -d/ -f1 | head -n1)

# Print interface and IP address of UE
printf 'UE interface and IP address\n'
printf 'UE_TUN=%s\nUE_IP=%s\n' "$UE_TUN" "$UE_IP"

# Get route decision on UE
printf '\nRouting table from UE to MEC server\n'
docker exec "$UE_CONTAINER" ip route get "$MEC_IP" from "$UE_IP"

# Ping MEC server from UE tunnel interface
printf '\nPing MEC server from UE tunnel interface\n'
docker exec "$UE_CONTAINER" ping -I "$UE_TUN" -c 5 "$MEC_IP"
