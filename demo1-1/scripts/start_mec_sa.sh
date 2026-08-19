#!/bin/bash
#
# start_mec_sa.sh
#
# Purpose: Script to be run on MEC PC.
#          Build the MEC server docker container without needing OAI 5GC.
#
# Prerequisites:
# - Install scripts must have been run with make prepare-mec-server-sa.
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


# Get script directory mec-server directory
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MEC_DIR="$(cd -- "${SCRIPT_DIR}/../mec-server" && pwd -P)"

# Build docker container
# Development mode: rebuild the MEC image before starting containers.
# This may make the digest recorded by build_mec_image.sh outdated.
log "Starting MEC server container..."
docker compose \
    --env-file "$MEC_DIR/.env" \
    -f "$MEC_DIR/compose.mec.sa.yml" \
    up -d --build ||
    die "Failed to start MEC server container."
log "MEC server started."
