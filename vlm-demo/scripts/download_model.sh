#!/usr/bin/env bash
#
# download_model.sh
#
# Purpose: Script to be run on MEC PC.
#          Downloads and verifies the ML model.
#
# Prerequisites:
# - build_mec_image.sh must already be run to build the MEC docker image.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

log() {
    printf '[download-model] %s\n' "$*"
}

die() {
    printf '[download-model] ERROR: %s\n' "$*" >&2
    exit 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

MEC_IMAGE="${MEC_IMAGE:-mec-vlm:latest}"
MODEL_ID="${MODEL_ID:-Qwen/Qwen3-VL-4B-Instruct}"

MODEL_ROOT="${DEMO_DIR}/mec/models"
HF_CACHE_DIR="${MODEL_ROOT}/huggingface"

command -v docker >/dev/null 2>&1 ||
    die "docker was not found in PATH"

docker image inspect "$MEC_IMAGE" >/dev/null 2>&1 ||
    die "Docker image does not exist: $MEC_IMAGE"

mkdir -p "$HF_CACHE_DIR" ||
    die "Unable to create model cache: $HF_CACHE_DIR"

[[ -w "$HF_CACHE_DIR" ]] ||
    die "Model cache is not writable: $HF_CACHE_DIR"

log "Downloading model: $MODEL_ID"
log "Cache directory: $HF_CACHE_DIR"

docker run --rm -i \
    --read-only \
    --user "$(id -u):$(id -g)" \
    -e USER="$(id -un)" \
    -e LOGNAME="$(id -un)" \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
    -e HOME=/tmp \
    -e HF_HOME=/models/huggingface \
    -e MODEL_ID="$MODEL_ID" \
    -v "$HF_CACHE_DIR:/models/huggingface" \
    --entrypoint /opt/mec-vlm-venv/bin/python \
    "$MEC_IMAGE" - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download
from transformers import AutoConfig, AutoProcessor

model_id = os.environ["MODEL_ID"]

snapshot_path = Path(
    snapshot_download(
        repo_id=model_id,
    )
)

if not snapshot_path.is_dir():
    raise SystemExit(f"Model snapshot was not created: {snapshot_path}")

# Validate that Transformers can resolve the downloaded files without network access.
AutoConfig.from_pretrained(
    model_id,
    local_files_only=True,
)
AutoProcessor.from_pretrained(
    model_id,
    local_files_only=True,
)

files = [path for path in snapshot_path.rglob("*") if path.is_file()]
if not files:
    raise SystemExit(f"Downloaded snapshot is empty: {snapshot_path}")

total_bytes = sum(path.stat().st_size for path in files)

print(f"Model: {model_id}")
print(f"Snapshot: {snapshot_path}")
print(f"Files: {len(files)}")
print(f"Snapshot bytes: {total_bytes}")
PY

# Ensure UID 10001 in the runtime container can traverse and read the cache,
# regardless of the UID that downloaded it.
chmod -R a+rX "$HF_CACHE_DIR" ||
    die "Failed to make the model cache readable"

log "Model download and validation completed."
