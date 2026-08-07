#!/bin/bash
#
# verify_mec_up.sh
#
# Purpose: Script to be run on MEC PC.
#          Verifies that the MEC server docker container is available listening on port 
#          8080 of the external data network docker container's N6 IP address.
#
# Prerequisites:
# - Must be run on PC running 5GC docker containers.
# - Script must be run in directory mec-offload-ad/demo1-1/scripts.
# - OAI 5GC docker containers must be running.
# - mec-yolo container must be running.
#
# Acknowledgement: Commands below were written by Generative AI.

set -e

# Check that the UPF and MEC docker containers are running.
UPF_IMAGE="${UPF_IMAGE:-oaisoftwarealliance/oai-upf:v2.1.10}"
MEC_IMAGE="${MEC_IMAGE:-mec-yolo:demo1-1}"

[[ -n "$(docker ps -q --filter "ancestor=$UPF_IMAGE" --filter "status=running")" ]] || { echo "ERROR: no active docker container found for image $UPF_IMAGE" >&2; exit 1; }
[[ -n "$(docker ps -q --filter "ancestor=$MEC_IMAGE" --filter "status=running")" ]] || { echo "ERROR: no active docker container found for image $MEC_IMAGE" >&2; exit 1; }

echo "Docker containers are active"

# Get PID of external data network container
MEC_CONTAINER=mec-yolo
MEC_IP=192.168.72.136
MEC_PID=$(docker inspect -f '{{.State.Pid}}' "$MEC_CONTAINER")

# Check that the MEC server is listening on port 8080 of the external data network
echo "Checking that model listening on port 8080"
LISTEN_OUTPUT=$(sudo nsenter -t "$MEC_PID" -n -- ss -ltnp | grep ':8080 ') || true

if [[ -z "$LISTEN_OUTPUT" ]]; then
  echo "ERROR: MEC server is not listening on port 8080" >&2
  exit 1
fi

printf '%s\n' "$LISTEN_OUTPUT"

# Check that MEC server can be reached.
echo "Checking that the MEC server is ready"
READY_OUTPUT=$(sudo nsenter -t "$MEC_PID" -n -- \
  curl --fail "http://${MEC_IP}:8080/readyz" | python3 -m json.tool | grep .) || true

if [[ -z "$READY_OUTPUT" ]]; then
  echo "ERROR: MEC server readiness check returned no output" >&2
  exit 1
fi

printf '%s\n' "$READY_OUTPUT"
