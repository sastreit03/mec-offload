#!/usr/bin/env bash
#
# build_ric_image.sh
#
# Purpose: Script to be run on MEC PC.
#          Builds the derived Flexric docker image and records its image digest.
#
# Prerequisites:
#  - Flexric image must exist for script to succeed.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[build-ric-image] %s\n' "$*"
}

die() {
    printf '[build-ric-image] ERROR: %s\n' "$*" >&2
    exit 1
}

# List directory names, digest file location, and docker image names
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
DOCKER_DIR="${DEMO_DIR}/mec/docker"
DIGEST_DIR="${DEMO_DIR}/install-logs/ric-image-digest"
DIGEST_FILE="${DIGEST_DIR}/ric-image-digest.txt"

BASE_IMAGE="oai-flexric:latest"
RIC_IMAGE="oai-flexric-grpc"
RIC_TAG="latest"
IMAGE="${RIC_IMAGE}:${RIC_TAG}"


# Check that base docker image exists
if docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    log "Base Docker image found: $BASE_IMAGE"
else
    die "Base Docker image not found locally: $BASE_IMAGE"
fi


# Build image
log "Building Flexric image..."
docker build \
    --file "$DOCKER_DIR/Dockerfile.flexric" \
    --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    --tag "$IMAGE" \
    "$DOCKER_DIR" ||
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
