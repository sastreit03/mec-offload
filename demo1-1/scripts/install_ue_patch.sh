#!/usr/bin/env bash
#
# install_ue_patch.sh
#
# Purpose: Script to be run on UE PC.
#          Applies the MEC UE image changes in ue_env_files.patch to each
#          UE .env file independently.
#          Copies the original compose file, checks the patch applies
#          cleanly, applies the patch, and validates the patch.
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
PATCH_FILE="${REPO_ROOT}/demo1-1/patches/ue_env_files.patch"
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

# Check existence of patch file
[[ -f "${PATCH_FILE}" ]] || die "Patch file not found: ${PATCH_FILE}"

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
    if ! git apply --check --include="${file}" --exclude='*' "${PATCH_FILE}" >/dev/null 2>&1; then
        failed+=("${file}")
        continue
    fi

    # Save the original once; never overwrite an existing original backup.
    if [[ ! -e "${backup}" ]]; then
        mkdir -p "$(dirname -- "${backup}")"
        cp --preserve=mode,timestamps "${file}" "${backup}"
    fi

    # Apply patch
    if git apply --include="${file}" --exclude='*' "${PATCH_FILE}" >/dev/null 2>&1; then
        log "Patched: ${file}"
    else
        failed+=("${file}")
    fi
done

# Exit procedures
if ((${#failed[@]})); then
    printf '[install-ue-patch] WARNING: The following patches did not apply:\n' >&2
    printf '  - %s\n' "${failed[@]}" >&2
    exit 1
fi

log "All UE .env patches are installed."
