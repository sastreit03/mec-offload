#!/usr/bin/env bash
# Stage an Ultralytics YOLO checkpoint outside the MEC application image.
#
# Typical use from mec-server/:
#   mkdir -p models
#   MEC_IMAGE=mec-yolo:demo1 \
#   MODEL_DIR="$PWD/models" \
#   MODEL_NAME=yolo11n.pt \
#   ../scripts/download_model.sh | tee model-stage.txt

set -Eeuo pipefail

MEC_IMAGE="${MEC_IMAGE:-mec-yolo:demo1-1}"
MODEL_DIR="${MODEL_DIR:-$PWD/models}"
MODEL_NAME="${MODEL_NAME:-yolo11n.pt}"
CHECKSUM_FILE="${CHECKSUM_FILE:-$MODEL_DIR/model.sha256}"
EXPECTED_SHA256="${EXPECTED_SHA256:-}"
FORCE="${FORCE:-0}"

log() {
    printf '[download_model] %s\n' "$*"
}

fail() {
    printf '[download_model] ERROR: %s\n' "$*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker was not found in PATH"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum was not found in PATH"

# MODEL_NAME is deliberately restricted to one plain checkpoint filename. This
# prevents path traversal and keeps the bind-mounted output location explicit.
case "$MODEL_NAME" in
    ""|*/*|*..*|*[!A-Za-z0-9._-]*)
        fail "MODEL_NAME must be a simple filename containing letters, digits, '.', '_' or '-'"
        ;;
esac
case "$MODEL_NAME" in
    *.pt) ;;
    *) fail "MODEL_NAME must end in .pt" ;;
esac

mkdir -p "$MODEL_DIR"
MODEL_DIR="$(cd "$MODEL_DIR" && pwd -P)"
TARGET="$MODEL_DIR/$MODEL_NAME"
CHECKSUM_FILE="${CHECKSUM_FILE/#\~/$HOME}"

[[ -w "$MODEL_DIR" ]] || fail "MODEL_DIR is not writable: $MODEL_DIR"
docker image inspect "$MEC_IMAGE" >/dev/null 2>&1 || \
    fail "Docker image $MEC_IMAGE does not exist; build the MEC image first"

write_and_verify_checksum() {
    local actual
    actual="$(sha256sum "$TARGET" | awk '{print $1}')"

    if [[ -n "$EXPECTED_SHA256" ]]; then
        [[ "$EXPECTED_SHA256" =~ ^[A-Fa-f0-9]{64}$ ]] || \
            fail "EXPECTED_SHA256 must contain exactly 64 hexadecimal characters"
        if [[ "${actual,,}" != "${EXPECTED_SHA256,,}" ]]; then
            fail "checksum mismatch: expected $EXPECTED_SHA256 but received $actual"
        fi
        log "Expected SHA-256 checksum verified"
    fi

    mkdir -p "$(dirname "$CHECKSUM_FILE")"
    printf '%s  %s\n' "$actual" "$MODEL_NAME" | tee "$CHECKSUM_FILE"
    log "Checkpoint: $TARGET"
    log "Checksum record: $CHECKSUM_FILE"
}

if [[ -s "$TARGET" && "$FORCE" != "1" ]]; then
    log "Checkpoint already exists; skipping download (set FORCE=1 to replace it)"
    write_and_verify_checksum
    exit 0
fi

if [[ "$FORCE" == "1" ]]; then
    log "FORCE=1; removing any existing checkpoint before staging"
    rm -f "$TARGET"
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

[[ -s "$TARGET" ]] || fail "download container completed but $TARGET is missing or empty"
write_and_verify_checksum
