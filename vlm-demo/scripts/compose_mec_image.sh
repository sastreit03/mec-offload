#!/usr/bin/env bash
#
# compose_mec_image.sh
#
# Purpose: Script to be run on MEC PC.
#          Validates the MEC docker compose configuration.
#
# Prerequisites:
# - build_mec_image.sh must already be run to build the MEC docker image
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[compose-mec-image] %s\n' "$*"
}

die() {
    printf '[compose-mec-image] ERROR: %s\n' "$*" >&2
    exit 1
}

# List file and directory names
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
MEC_DIR="${DEMO_DIR}/mec"
RENDERED_DIR="${DEMO_DIR}/install-logs/image-compose"

COMPOSE_FILE="${MEC_DIR}/compose.mec.yml"
RENDERED_FILE="${RENDERED_DIR}/compose.mec.rendered.yml"


# MEC image name
MEC_IMAGE="${MEC_IMAGE:-mec-vlm:latest}"


# Check that docker is available and that MEC image is available.
docker compose version >/dev/null 2>&1 ||
    die "Docker Compose is unavailable"

docker image inspect "$MEC_IMAGE" >/dev/null 2>&1 ||
    die "Docker image is not built: $MEC_IMAGE"
log "Found docker image: $MEC_IMAGE"


# Check that docker-compose file exists
[[ -f "$COMPOSE_FILE" ]] ||
    die "Compose file not found: $COMPOSE_FILE"

log "Found docker-compose file."

# Make directory where output yml goes
mkdir -p "$RENDERED_DIR" ||
    die "Failed to create output directory: $RENDERED_DIR"

[[ -w "$RENDERED_DIR" ]] ||
    die "Output directory is not writable: $RENDERED_DIR"


# Compose docker image
cd "$MEC_DIR"

docker compose -f compose.mec.yml config |
    tee "$RENDERED_FILE" ||
    die "Failed to render Compose configuration"

# Check that rendered file is not empty
[[ -s "$RENDERED_FILE" ]] ||
    die "Rendered Compose file is empty: $RENDERED_FILE"

log "Rendering of $MEC_IMAGE complete."
log "Rendered Compose file: $RENDERED_FILE"
