#!/usr/bin/env bash
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


# Check that UPF container is running
UPF_CONTAINER="${UPF_CONTAINER:-oai-upf}"

log "Checking that UPF container is running..."
[[ "$(docker inspect -f '{{.State.Running}}' "$UPF_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $UPF_CONTAINER"
log "Container is running: $UPF_CONTAINER"


# Get script directory mec-server directory
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DOCKER_DIR="$(cd -- "${SCRIPT_DIR}/../mec/docker" && pwd -P)"

# Build docker container
# Development mode: rebuild the MEC image before starting containers.
# This may make the digest recorded by build_mec_image.sh outdated.
log "Starting MEC server container..."
docker compose \
    -f "$DOCKER_DIR/compose.mec.yml" \
    up -d --build ||
    die "Failed to start MEC server container."
log "MEC server started."
