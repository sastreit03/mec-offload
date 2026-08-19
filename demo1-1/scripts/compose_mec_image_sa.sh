#!/usr/bin/env bash
#
# compose_mec_image_sa.sh
#
# Purpose: Script to be run on MEC PC.
#          Validates the MEC docker compose configuration.
#
# Prerequisites:
# - build_mec_image_sa.sh must already be run to build the MEC docker image
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
MEC_DIR="${DEMO_DIR}/mec-server"
RENDERED_DIR="${DEMO_DIR}/install-logs/image-compose-sa"

ENV_FILE="${MEC_DIR}/.env"
COMPOSE_FILE="${MEC_DIR}/compose.mec.sa.yml"
RENDERED_FILE="${RENDERED_DIR}/compose.mec.sa.rendered.yml"


# MEC image name
MEC_IMAGE="${MEC_IMAGE:-mec-yolo:latest}"


# Check that docker is available and that MEC image is available.
docker compose version >/dev/null 2>&1 ||
    die "Docker Compose is unavailable"

docker image inspect "$MEC_IMAGE" >/dev/null 2>&1 ||
    die "Docker image is not built: $MEC_IMAGE"
log "Found docker image: $MEC_IMAGE"


# Check that .env and docker-compose files exist
[[ -f "$ENV_FILE" ]] ||
    die "Environment file not found: $ENV_FILE"

[[ -f "$COMPOSE_FILE" ]] ||
    die "Compose file not found: $COMPOSE_FILE"

log "Found .env and docker-compose files."

# Make directory where output yml goes
mkdir -p "$RENDERED_DIR" ||
    die "Failed to create output directory: $RENDERED_DIR"

[[ -w "$RENDERED_DIR" ]] ||
    die "Output directory is not writable: $RENDERED_DIR"


# Compose docker image
cd "$MEC_DIR"

docker compose --env-file .env -f compose.mec.sa.yml config |
    tee "$RENDERED_FILE" ||
    die "Failed to render Compose configuration"

# Check that rendered file is not empty
[[ -s "$RENDERED_FILE" ]] ||
    die "Rendered Compose file is empty: $RENDERED_FILE"

log "Rendering of $MEC_IMAGE complete."
log "Rendered Compose file: $RENDERED_FILE"
