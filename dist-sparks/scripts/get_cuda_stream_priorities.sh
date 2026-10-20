#!/usr/bin/env bash
#
# get_cuda_stream_priorities.sh
#
# Purpose: Script to be run on MEC PC.
#          Displays CUDA Stream priorities of the Nvidia
#          pytorch image.
#
# Prerequisites:
# - verify_gpu.sh must already be run to install
#   nvcr.io/nvidia/pytorch:25.12-py3 image.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[get-cuda-stream-priorities] %s\n' "$*"
}

die() {
    printf '[get-cuda-stream-priorities] ERROR: %s\n' "$*" >&2
    exit 1
}


# List pytorch docker image name
PYTORCH_IMAGE="${PYTORCH_IMAGE:-nvcr.io/nvidia/pytorch:25.12-py3}"

# Check that base docker image exists
if docker image inspect "$PYTORCH_IMAGE" >/dev/null 2>&1; then
    log "PyTorch Docker image found: $PYTORCH_IMAGE"
else
    die "PyTorch Docker image not found locally: $PYTORCH_IMAGE"
fi


# Run python command to get priorities
docker run --rm -i --gpus all "$PYTORCH_IMAGE" python - <<'PY'
import ctypes as ct
import torch

torch.cuda.set_device(0)
torch.cuda.init()

cuda = ct.CDLL("libcudart.so")
cuda.cudaGetErrorString.argtypes = [ct.c_int]
cuda.cudaGetErrorString.restype = ct.c_char_p
cuda.cudaDeviceGetStreamPriorityRange.argtypes = [
    ct.POINTER(ct.c_int), ct.POINTER(ct.c_int)
]
cuda.cudaDeviceGetStreamPriorityRange.restype = ct.c_int

least = ct.c_int()
greatest = ct.c_int()
err = cuda.cudaDeviceGetStreamPriorityRange(
    ct.byref(least), ct.byref(greatest)
)
if err:
    raise RuntimeError(cuda.cudaGetErrorString(err).decode())

print("GPU:", torch.cuda.get_device_name(0))
print("PyTorch:", torch.__version__)
print("PyTorch CUDA build:", torch.version.cuda)
print("Lowest scheduling priority:", least.value)
print("Highest scheduling priority:", greatest.value)
print(f"Valid numerical range: [{greatest.value}, {least.value}]")
PY
