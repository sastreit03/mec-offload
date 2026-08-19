#!/bin/bash
#
# start_mec.sh
#
# Purpose: Script to be run on MEC PC.
#          Build the MEC server docker container.
#
# Prerequisites:
# - OAI 5GC docker containers must be running.
# - Every time the OAI 5GC containers are removed, the MEC container
#   must be restarted, too.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail  # Stop script on any error

# Helper functions
log() {
    printf '[start-mec] %s\n' "$*"
}

die() {
    printf '[start-mec] ERROR: %s\n' "$*" >&2
    exit 1
}


# Check that UPF and gNB containers are running
UPF_CONTAINER="${UPF_CONTAINER:-oai-upf}"
GNB_CONTAINER="${GNB_CONTAINER:-oai-gnb}"

log "Checking that UPF and gNB containers are running..."
for container in "$UPF_CONTAINER" "$GNB_CONTAINER"; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" == "true" ]] ||
        die "Container is missing or not running: $container"
    log "Container is running: $container"
done


# Get script directory mec-server directory
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MEC_DIR="$(cd -- "${SCRIPT_DIR}/../mec-server" && pwd -P)"

# Build docker container
# Development mode: rebuild the MEC image before starting containers.
# This may make the digest recorded by build_mec_image.sh outdated.
log "Starting MEC server container..."
docker compose \
    --env-file "$MEC_DIR/.env" \
    -f "$MEC_DIR/compose.mec.yml" \
    up -d --build ||
    die "Failed to start MEC server container."
log "MEC server started."
