#!/usr/bin/env bash
#
# download_model.sh
#
# Purpose: Script to be run on MEC PC.
#          Downloads and verifies the ML model and records its SHA-256 checksum.
#
# Prerequisites:
# - build_mec_image.sh must already be run to build the MEC docker image.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[download-model] %s\n' "$*"
}

die() {
    printf '[download-model] ERROR: %s\n' "$*" >&2
    exit 1
}


# List parameters
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

MEC_IMAGE="${MEC_IMAGE:-mec-yolo:latest}"

MODEL_DIR="${DEMO_DIR}/mec-server/models"
MODEL_NAME="${MODEL_NAME:-yolo11n.pt}"
TARGET="$MODEL_DIR/$MODEL_NAME"

CHECKSUM_DIR="${DEMO_DIR}/install-logs/model-checksums"
CHECKSUM_FILE="${CHECKSUM_DIR}/${MODEL_NAME}.sh256"
EXPECTED_SHA256="${EXPECTED_SHA256:-}"
FORCE="${FORCE:-0}"


# Check that docker and sha256sum are available.
command -v docker >/dev/null 2>&1 || die "docker was not found in PATH"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum was not found in PATH"
log "Verified that docker and sha256sum are in PATH."


# MODEL_NAME is deliberately restricted to one plain checkpoint filename. This
# prevents path traversal and keeps the bind-mounted output location explicit.
case "$MODEL_NAME" in
    ""|*/*|*..*|*[!A-Za-z0-9._-]*)
        die "MODEL_NAME must be a simple filename containing letters, digits, '.', '_' or '-'"
        ;;
esac
case "$MODEL_NAME" in
    *.pt) ;;
    *) die "MODEL_NAME must end in .pt" ;;
esac


# Make model and checksum directories if they don't exist. Check that they are writable.
mkdir -p "$MODEL_DIR" "$CHECKSUM_DIR" ||
    die "Failed to create model or checksum directory"

[[ -w "$MODEL_DIR" ]] ||
    die "Model directory is not writable: $MODEL_DIR"

[[ -w "$CHECKSUM_DIR" ]] ||
    die "Checksum directory is not writable: $CHECKSUM_DIR"


# Check that the docker is built
docker image inspect "$MEC_IMAGE" >/dev/null 2>&1 || \
    die "Docker image $MEC_IMAGE does not exist; build the MEC image first."
log "Docker image $MEC_IMAGE found."



# Helper function to verify checksum and write it to log file
write_and_verify_checksum() {
    local actual

    actual="$(sha256sum "$TARGET")" ||
        die "Failed to calculate checksum: $TARGET"
    actual="${actual%% *}"

    if [[ -n "$EXPECTED_SHA256" ]]; then
        [[ "$EXPECTED_SHA256" =~ ^[A-Fa-f0-9]{64}$ ]] ||
            die "EXPECTED_SHA256 must contain exactly 64 hexadecimal characters"

        [[ "${actual,,}" == "${EXPECTED_SHA256,,}" ]] ||
            die "Checksum mismatch: expected $EXPECTED_SHA256, received $actual"
    fi

    if [[ ! -f "$CHECKSUM_FILE" ]]; then
        printf '%s  %s\n' "$actual" "$MODEL_NAME" >"$CHECKSUM_FILE" ||
            die "Failed to write checksum: $CHECKSUM_FILE"
    fi

    (cd "$MODEL_DIR" && sha256sum --check "$CHECKSUM_FILE") ||
        die "Checksum verification failed: $MODEL_NAME"

    log "Checkpoint: $TARGET"
    log "Checksum: $CHECKSUM_FILE"
}


# Validate FORCE parameter
case "$FORCE" in
    0|1) ;;
    *) die "FORCE parameter must be 0 or 1" ;;
esac

# If force is 0 (default), don't download the model if it exists.
if [[ -s "$TARGET" && -f "$CHECKSUM_FILE" && "$FORCE" != "1" ]]; then
    log "Checkpoint already exists; skipping download (set FORCE=1 to replace it)"
    write_and_verify_checksum
    exit 0
fi

# If force is 1, remove the model and proceed to download it.
if [[ "$FORCE" == "1" ]]; then
    log "FORCE=1; removing existing checkpoint and checksum"
    rm -f "$TARGET" "$CHECKSUM_FILE" ||
        die "Failed to remove existing model artifacts"
fi

log "Staging $MODEL_NAME with container image $MEC_IMAGE"
log "Output directory: $MODEL_DIR"


# Run the download through the already-built MEC image so the exact installed
# Ultralytics version performs checkpoint resolution. The container runs with
# the invoking host UID/GID so the resulting file is not owned by root.
docker run --rm -i \
    --user "$(id -u):$(id -g)" \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g \
    -e HOME=/tmp \
    -e YOLO_CONFIG_DIR=/tmp \
    -e MODEL_NAME="$MODEL_NAME" \
    -v "$MODEL_DIR:/models" \
    -w /models \
    --entrypoint python \
    "$MEC_IMAGE" - <<'PY'
import os
import shutil
from pathlib import Path

from ultralytics import YOLO

model_name = os.environ["MODEL_NAME"]
destination = Path("/models") / model_name

# For an official name such as yolo11n.pt, Ultralytics downloads the checkpoint
# when it is not already present and then loads it to validate the artifact.
model = YOLO(model_name)

if not destination.is_file():
    checkpoint = getattr(model, "ckpt_path", None)
    checkpoint_path = Path(str(checkpoint)) if checkpoint else None
    if checkpoint_path and checkpoint_path.is_file():
        shutil.copy2(checkpoint_path, destination)

if not destination.is_file() or destination.stat().st_size == 0:
    raise SystemExit(f"checkpoint was not staged at {destination}")

# Load the final mounted path once more so a truncated or unreadable artifact
# fails during staging rather than when the MEC service starts.
YOLO(str(destination))
print(f"staged {destination.name} ({destination.stat().st_size} bytes)")
PY

[[ -s "$TARGET" ]] || die "Download container completed but $TARGET is missing or empty."
write_and_verify_checksum
