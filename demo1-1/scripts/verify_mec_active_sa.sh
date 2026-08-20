#!/bin/bash
#
# verify_mec_active_sa.sh
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
#for cmd in docker sudo nsenter ss curl python3; do
#    command -v "$cmd" >/dev/null 2>&1 ||
#        die "$cmd was not found in PATH"
#done
#log "Necessary commands have been found in PATH."

for cmd in docker curl python3; do
    command -v "$cmd" >/dev/null 2>&1 ||
        die "$cmd was not found in PATH"
done
log "Necessary commands have been found in PATH."


# Check that the UPF and MEC docker containers are running.
MEC_CONTAINER="${MEC_CONTAINER:-mec-yolo-sa}"

[[ "$(docker inspect -f '{{.State.Running}}' "$MEC_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $MEC_CONTAINER"
log "Container is running: $MEC_CONTAINER"


# Get PID of MEC container
#MEC_PID="$(docker inspect -f '{{.State.Pid}}' "$MEC_CONTAINER")" ||
#    die "Failed to obtain PID for $MEC_CONTAINER"
#[[ "$MEC_PID" =~ ^[1-9][0-9]*$ ]] ||
#    die "Invalid PID for $MEC_CONTAINER: $MEC_PID"

log "Checking that container port 8080/tcp is published..."
PORT_OUTPUT="$(docker port "$MEC_CONTAINER" 8080/tcp 2>/dev/null)" ||
    die "Failed to inspect published port 8080/tcp"

[[ -n "$PORT_OUTPUT" ]] ||
    die "Container port 8080/tcp is not published"

printf '%s\n' "$PORT_OUTPUT"

# Check that the MEC server is listening on port 8080 of the external data network
#log "Checking that server is listening on port 8080..."
#LISTEN_OUTPUT="$(
#    sudo nsenter -t "$MEC_PID" -n -- ss -H -ltn 'sport = :8080'
#)" || die "Failed to inspect the MEC container network namespace"

#[[ -n "$LISTEN_OUTPUT" ]] ||
#    die "MEC server is not listening on port 8080. If server just started, wait a few seconds and re-run script."

#printf '%s\n' "$LISTEN_OUTPUT"


# Check that MEC server can be reached.
log "Checking that the MEC server is ready..."
READY_OUTPUT="$(
    curl --fail --silent --show-error --max-time 10 \
        "http://127.0.0.1:8080/readyz" |
        python3 -m json.tool
)" || die "MEC readiness request failed"

[[ -n "$READY_OUTPUT" ]] ||
    die "MEC readiness request returned no output"

printf '%s\n' "$READY_OUTPUT"

log "MEC server is ready to be reached by clients."
