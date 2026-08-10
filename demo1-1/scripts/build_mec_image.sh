#!/usr/bin/env bash
#
# build_mec_image.sh
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


# List directory names, docker image names, and digest file location
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MEC_DIR="${SCRIPT_DIR}/../mec-server"
DIGEST_DIR="${SCRIPT_DIR}/../install-logs/mec-image-digest"

BASE_IMAGE="nvcr.io/nvidia/pytorch:25.12-py3"
MEC_IMAGE="mec-yolo"
MEC_TAG="latest"
IMAGE="${MEC_IMAGE}:${MEC_TAG}"
DIGEST_FILE="${DIGEST_DIR}/mec-image-digest.txt"


# Build image
log "Building MEC image..."
cd "$MEC_DIR"
docker build \
    --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    --tag "$IMAGE" \
    . ||
    die "Failed to build Docker image: $IMAGE"
log "Built image: $IMAGE"


# Record local image digest
log "Recording local image digest..."
mkdir -p "$DIGEST_DIR"
docker image inspect --format '{{.Id}}' "$IMAGE" >"$DIGEST_FILE" ||
    die "Failed to inspect Docker image: $IMAGE"
log "Image digest saved to: $DIGEST_FILE"