#!/usr/bin/env bash
#
# install_ue_patch.sh
#
# Purpose: Script to be run on UE PC.
#          Applies the MEC UE image changes in ue_env_files.patch to each
#          UE .env file independently and publishes the MEC application port
#          with ue_publish_port.patch.
#          Copies the original files, checks each patch applies cleanly,
#          applies the patches, and validates the results.
#
# Prerequisites:
# - OAI/Sionna RK must already be installed.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[install-ue-patch] %s\n' "$*"
}

die() {
    printf '[install-ue-patch] ERROR: %s\n' "$*" >&2
    exit 1
}

# Get files and directories
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
ENV_PATCH_FILE="${REPO_ROOT}/vlm-demo/patches/ue_env_files.patch"
COMPOSE_PATCH_FILE="${REPO_ROOT}/vlm-demo/patches/ue_publish_port.patch"
COMPOSE_FILE="ue/config/common/docker-compose.yaml"
BACKUP_DIR="${REPO_ROOT}/original-srk-files/ue/config"

# List of files to patch
FILES=(
    ue/config/b200/.env
    ue/config/rfsim/.env
    ue/config/testing/.env
    ue/config/x410/.env
)

# Check that git is installed
command -v git >/dev/null 2>&1 || die "git is not installed."

# Check existence of patch files
[[ -f "${ENV_PATCH_FILE}" ]] || die "Patch file not found: ${ENV_PATCH_FILE}"
[[ -f "${COMPOSE_PATCH_FILE}" ]] || die "Patch file not found: ${COMPOSE_PATCH_FILE}"

# Check that repo root directory is a Git working tree
git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    die "Repository root is not a Git working tree: ${REPO_ROOT}"

cd "${REPO_ROOT}"
mkdir -p "${BACKUP_DIR}"

failed=()

for file in "${FILES[@]}"; do
    backup="${BACKUP_DIR}/${file#ue/config/}"

    # Check existence of file
    if [[ ! -f "${file}" ]]; then
        failed+=("${file} (file not found)")
        continue
    fi

    # Treat an already-patched file as success.
    if grep -Fxq 'UE_IMAGE=oai-nr-ue-cuda-mec' "${file}" &&
        grep -Fxq 'UE_TAG=latest' "${file}"; then
        log "Already patched: ${file}"
        continue
    fi

    # Check this file's hunk independently so another failure does not stop us.
    if ! git apply --check --include="${file}" --exclude='*' "${ENV_PATCH_FILE}" >/dev/null 2>&1; then
        failed+=("${file}")
        continue
    fi

    # Save the original once; never overwrite an existing original backup.
    if [[ ! -e "${backup}" ]]; then
        mkdir -p "$(dirname -- "${backup}")"
        cp --preserve=mode,timestamps "${file}" "${backup}"
    fi

    # Apply patch
    if git apply --include="${file}" --exclude='*' "${ENV_PATCH_FILE}" >/dev/null 2>&1; then
        log "Patched: ${file}"
    else
        failed+=("${file}")
    fi
done

# Apply the port-publishing patch to the UE service in the compose file.
compose_backup="${BACKUP_DIR}/${COMPOSE_FILE#ue/config/}"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
    failed+=("${COMPOSE_FILE} (file not found)")
elif awk '
    /^    oai-nr-ue:$/ { in_ue_service = 1; next }
    in_ue_service && /^    [^[:space:]]/ { exit }
    in_ue_service && /^[[:space:]]+- "8080:8080\/tcp"[[:space:]]*$/ { found = 1 }
    END { exit !found }
' "${COMPOSE_FILE}"; then
    log "Already patched: ${COMPOSE_FILE}"
elif ! git apply --check --include="${COMPOSE_FILE}" --exclude='*' "${COMPOSE_PATCH_FILE}" >/dev/null 2>&1; then
    failed+=("${COMPOSE_FILE}")
else
    # Save the original once; never overwrite an existing original backup.
    if [[ ! -e "${compose_backup}" ]]; then
        mkdir -p "$(dirname -- "${compose_backup}")"
        cp --preserve=mode,timestamps "${COMPOSE_FILE}" "${compose_backup}"
    fi

    if git apply --include="${COMPOSE_FILE}" --exclude='*' "${COMPOSE_PATCH_FILE}" >/dev/null 2>&1; then
        log "Patched: ${COMPOSE_FILE}"
    else
        failed+=("${COMPOSE_FILE}")
    fi
fi

# Exit procedures
if ((${#failed[@]})); then
    printf '[install-ue-patch] WARNING: The following patches did not apply:\n' >&2
    printf '  - %s\n' "${failed[@]}" >&2
    exit 1
fi

log "All UE patches are installed."
