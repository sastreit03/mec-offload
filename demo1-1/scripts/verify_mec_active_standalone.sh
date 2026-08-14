#!/bin/bash
#
# verify_mec_active_standalone.sh
#
# Purpose: Script to be run on MEC PC.
#          Verifies that the MEC server docker container has the correct IP
#          address, is listening on port 8080 of the MEC container, and is 
#          ready to service clients.
#
# Prerequisites:
# - mec-yolo container must be running.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[verify-mec-active] %s\n' "$*"
}

die() {
    printf '[verify-mec-active] ERROR: %s\n' "$*" >&2
    exit 1
}


# Check that commands are in PATH
for cmd in docker sudo nsenter ss curl python3; do
    command -v "$cmd" >/dev/null 2>&1 ||
        die "$cmd was not found in PATH"
done
log "Necessary commands have been found in PATH."


# Check that the UPF and MEC docker containers are running.
MEC_CONTAINER="${MEC_CONTAINER:-mec-yolo}"

[[ "$(docker inspect -f '{{.State.Running}}' "$MEC_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $MEC_CONTAINER"
log "Container is running: $MEC_CONTAINER"


# Check that the MEC server has the correct IP address and network
MEC_NETWORK="${MEC_NETWORK:-oai-traffic-net}"
MEC_IP="${MEC_IP:-192.168.72.136}"

# Check network
ATTACHED="$(docker inspect --format \
    "{{if index .NetworkSettings.Networks \"${MEC_NETWORK}\"}}true{{else}}false{{end}}" \
    "$MEC_CONTAINER")" ||
    die "Failed to inspect container: $MEC_CONTAINER"

[[ "$ATTACHED" == "true" ]] ||
    die "$MEC_CONTAINER is not connected to $MEC_NETWORK"

# Check IP address
ACTUAL_IP="$(docker inspect --format \
    "{{(index .NetworkSettings.Networks \"${MEC_NETWORK}\").IPAddress}}" \
    "$MEC_CONTAINER")" ||
    die "Failed to inspect IP address for $MEC_CONTAINER"

[[ "$ACTUAL_IP" == "$MEC_IP" ]] ||
    die "$MEC_CONTAINER has IP $ACTUAL_IP; expected $MEC_IP"

log "$MEC_CONTAINER is connected to $MEC_NETWORK with IP $MEC_IP"


# Get PID of MEC container
MEC_PID="$(docker inspect -f '{{.State.Pid}}' "$MEC_CONTAINER")" ||
    die "Failed to obtain PID for $MEC_CONTAINER"
[[ "$MEC_PID" =~ ^[1-9][0-9]*$ ]] ||
    die "Invalid PID for $MEC_CONTAINER: $MEC_PID"


# Check that the MEC server is listening on port 8080 of the external data network
log "Checking that server is listening on port 8080..."
LISTEN_OUTPUT="$(
    sudo nsenter -t "$MEC_PID" -n -- ss -H -ltn 'sport = :8080'
)" || die "Failed to inspect the MEC container network namespace"

[[ -n "$LISTEN_OUTPUT" ]] ||
    die "MEC server is not listening on port 8080. If server just started, wait a few seconds and rerun script."

printf '%s\n' "$LISTEN_OUTPUT"


# Check that MEC server can be reached.
log "Checking that the MEC server is ready..."
READY_OUTPUT="$(
    sudo nsenter -t "$MEC_PID" -n -- \
        curl --fail --silent --show-error --max-time 10 \
        "http://${MEC_IP}:8080/readyz" |
        python3 -m json.tool
)" || die "MEC readiness request failed"

[[ -n "$READY_OUTPUT" ]] ||
    die "MEC readiness request returned no output"

printf '%s\n' "$READY_OUTPUT"

log "MEC server is ready to be reached by clients."
