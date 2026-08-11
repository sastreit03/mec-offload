#!/usr/bin/env bash
#
# verify_gpu.sh
#
# Purpose: Script to be run on MEC PC.
#          Determines if drivers detect GPU and if docker images can use
#          the GPU. Downloads the Nvidia Pytorch image required for
#          proper operation of MEC.
#
# Note:
#   - Downloads Nvidia Cuda image to test GPU usage in docker container.
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[verify-gpu] %s\n' "$*"
}

die() {
    printf '[verify-gpu] ERROR: %s\n' "$*" >&2
    exit 1
}

ensure_image() {
    if docker image inspect "$1" >/dev/null 2>&1; then
        log "Image already present: $1"
    else
        log "Downloading image: $1"
        docker pull "$1" || die "Failed to download image: $1"
    fi
}


# Set docker image names
CUDA_IMAGE="nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04"
PYTORCH_IMAGE="nvcr.io/nvidia/pytorch:25.12-py3"


# Verify nvidia-smi can communicate with GPU
log "Verifying nvidia-smi..."
nvidia-smi ||
    die "nvidia-smi failed on host PC."

# Verify nvidia-ctk is present
log "Getting nvidia-ctk version..."
nvidia-ctk --version ||
    die "nvidia-ctk failed on host PC."

# Download Nvidia GPU docker image and test that it can use GPU
log "Pulling Nvidia GPU docker image and verifying GPU access..."
ensure_image "$CUDA_IMAGE"

docker run --rm --gpus=all "$CUDA_IMAGE" nvidia-smi ||
    die "GPU access failed in CUDA container"
log "GPU access in docker verified."


# Download Nvidia Pytorch image
log "Pulling Nvidia Pytorch image..."
ensure_image "$PYTORCH_IMAGE"

if ! docker run --rm --gpus=all -i "$PYTORCH_IMAGE" python - <<'PY'
import torch

print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)

assert torch.cuda.is_available()
PY
then
    die "GPU access failed in PyTorch container"
fi

log "Pytorch image verified. GPU verification completed successfully"
