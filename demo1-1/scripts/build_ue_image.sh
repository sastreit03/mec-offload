#!/usr/bin/env bash
#
# build_ue_image.sh
#
# Purpose: Script to be run on UE PC.
#          Builds the UE client docker image and records its image digest.
#
# Prerequisites:
# - verify_srk_ue.sh must already be run to verify base UE image exists.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[build-ue-image] %s\n' "$*"
}

die() {
    printf '[build-ue-image] ERROR: %s\n' "$*" >&2
    exit 1
}


# List directory names, digest file location, and docker image names
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
UE_DIR="${DEMO_DIR}/ue-client"
DIGEST_DIR="${DEMO_DIR}/install-logs/ue-image-digest"
DIGEST_FILE="${DIGEST_DIR}/ue-image-digest.txt"

BASE_IMAGE="${BASE_IMAGE:-oai-nr-ue-cuda:latest}"
UE_IMAGE="${UE_IMAGE:-oai-nr-ue-cuda-mec}"
UE_TAG="${UE_TAG:-latest}"
IMAGE="${UE_IMAGE}:${UE_TAG}"


# Check that local UE image exists
if docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    log "Docker image found: $BASE_IMAGE"
else
    die "Docker image not found locally: $BASE_IMAGE"
fi


# Check that Dockerfile and its requirements.txt file exist
[[ -f "$UE_DIR/Dockerfile" ]] ||
    die "Dockerfile not found: $UE_DIR/Dockerfile"
log "Found Dockerfile."

[[ -f "$UE_DIR/requirements.txt" ]] ||
    die "Requirements file not found: $UE_DIR/requirements.txt"
log "Found requirements.txt."

# Build UE image
log "Building UE image..."
docker build \
    --file "$UE_DIR/Dockerfile" \
    --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    --tag "$IMAGE" \
    "$UE_DIR" ||
    die "Failed to build Docker image: $IMAGE"
log "Built image: $IMAGE"


# Record local image digest
log "Recording local image digest..."
mkdir -p "$DIGEST_DIR" || die "Failed to create digest directory: $DIGEST_DIR"

DIGEST="$(docker image inspect --format '{{.Id}}' "$IMAGE")" ||
    die "Failed to inspect Docker image: $IMAGE"

printf '%s\n' "$DIGEST" >"$DIGEST_FILE" ||
    die "Failed to write digest file: $DIGEST_FILE"

log "Image digest saved to: $DIGEST_FILE"