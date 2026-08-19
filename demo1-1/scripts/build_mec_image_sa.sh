#!/usr/bin/env bash
#
# build_mec_image_sa.sh
#
# Purpose: Script to be run on MEC PC.
#          Builds the MEC docker image and records its image digest.
#
# Prerequisites:
# - verify_gpu.sh must already be run to install
#   nvcr.io/nvidia/pytorch:25.12-py3 image.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[build-mec-image] %s\n' "$*"
}

die() {
    printf '[build-mec-image] ERROR: %s\n' "$*" >&2
    exit 1
}


# List directory names, digest file location, and docker image names
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
MEC_DIR="${DEMO_DIR}/mec-server"
DIGEST_DIR="${DEMO_DIR}/install-logs/mec-image-sa-digest"
DIGEST_FILE="${DIGEST_DIR}/mec-image-sa-digest.txt"

BASE_IMAGE="nvcr.io/nvidia/pytorch:25.12-py3"
MEC_IMAGE="mec-yolo"
MEC_TAG="latest"
IMAGE="${MEC_IMAGE}:${MEC_TAG}"


# Build image
log "Building MEC image..."
docker build \
    --file "$MEC_DIR/Dockerfile" \
    --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    --tag "$IMAGE" \
    "$MEC_DIR" ||
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
