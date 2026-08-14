#!/bin/bash
#
# start_mec_standalone.sh
#
# Purpose: Script to be run on MEC PC.
#          Build the MEC server docker container without needing OAI 5GC.
#
# Prerequisites:
# - Install scripts must have been run with make prepare-mec-server-standalone.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail  # Stop script on any error

# Helper functions
log() {
    printf '[build-mec-image] %s\n' "$*"
}

die() {
    printf '[build-mec-image] ERROR: %s\n' "$*" >&2
    exit 1
}

# Commented out below because standalone deployment does not use OAI's stack.
# Check that UPF and gNB containers are running
#UPF_CONTAINER="${UPF_CONTAINER:-oai-upf}"
#GNB_CONTAINER="${GNB_CONTAINER:-oai-gnb}"

#log "Checking that UPF and gNB containers are running..."
#for container in "$UPF_CONTAINER" "$GNB_CONTAINER"; do
#    [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" == "true" ]] ||
#        die "Container is missing or not running: $container"
#    log "Container is running: $container"
#done


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
