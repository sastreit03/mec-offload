#!/bin/bash
#
# verify_mec_up.sh
#
# Purpose: Verifies that the MEC server docker container is available listening on port 
#          8080 of the external data network docker container's N6 IP address.
#
# Prerequisites:
# - Must be run on PC running 5GC docker containers.
# - Script must be run in directory mec-offload-ad/demo1/scripts.
# - OAI 5GC docker containers must be running.
# - mec-yolo container must be running.
#
# Acknowledgement: Commands below were written by Generative AI.

set -e

# Check that the UPF and MEC docker containers are running.
EXT_DN_IMAGE="${EXT_DN_IMAGE:-oaisoftwarealliance/trf-gen-cn5g:latest}"
UPF_IMAGE="${UPF_IMAGE:-oaisoftwarealliance/oai-upf:v2.1.10}"
MEC_IMAGE="${MEC_IMAGE:-mec-yolo:demo1}"

[[ -n "$(docker ps -q --filter "ancestor=$EXT_DN_IMAGE")" ]] || { echo "ERROR: no active docker container found for image $EXT_DN_IMAGE" >&2; exit 1; }
[[ -n "$(docker ps -q --filter "ancestor=$UPF_IMAGE")" ]] || { echo "ERROR: no active docker container found for image $UPF_IMAGE" >&2; exit 1; }
[[ -n "$(docker ps -q --filter "ancestor=$MEC_IMAGE")" ]] || { echo "ERROR: no active docker container found for image $MEC_IMAGE" >&2; exit 1; }

echo "Docker containers are active"

# Get PID of external data network container
EXT_DN_CONTAINER=oai-ext-dn
MEC_IP=192.168.72.135
EXT_DN_PID=$(docker inspect -f '{{.State.Pid}}' "$EXT_DN_CONTAINER")

# Check that the MEC server is listening on port 8080 of the external data network
sudo nsenter -t "$EXT_DN_PID" -n -- ss -ltnp | grep ':8080 '

# Check that MEC server can be reached.
sudo nsenter -t "$EXT_DN_PID" -n -- \
  curl --fail "http://${MEC_IP}:8080/readyz" | python3 -m json.tool | grep .
