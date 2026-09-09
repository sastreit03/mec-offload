#!/usr/bin/env bash
#
# start_xapp.sh
#
# Purpose: Script to be run on gNB PC.
#          Build the MEC server docker container.
#
# Prerequisites:
# - OAI gNB and nearRT-RIC docker containers must be running.
# - Every time the OAI containers are removed, the xApp container
#   must be restarted, too.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail  # Stop script on any error

# Helper functions
log() {
    printf '[start-xapp] %s\n' "$*"
}

die() {
    printf '[start-xapp] ERROR: %s\n' "$*" >&2
    exit 1
}


# Check that gNB and nearRT-RIC container is running
GNB_CONTAINER="${GNB_CONTAINER:-oai-gnb}"
RIC_CONTAINER="${RIC_CONTAINER:-nearRT-RIC}"

log "Checking that gNB and nearRT-RIC containers are running..."
for container in "$GNB_CONTAINER" "$RIC_CONTAINER"; do
    [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" == "true" ]] ||
        die "Container is missing or not running: $container"
    log "Container is running: $container"
done


# Get script and docker directories
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DOCKER_DIR="$(cd -- "${SCRIPT_DIR}/../xapp/docker" && pwd -P)"

# Build docker container
# Development mode: rebuild the xApp image before starting containers.
# This may make the digest recorded by build_ric_image.sh outdated.
log "Starting xApp container..."
docker compose \
    --env-file "$DOCKER_DIR/.env" \
    -f "$DOCKER_DIR/compose.xapp.yml" \
    up -d --build ||
    die "Failed to start xApp container."
log "xApp started."
