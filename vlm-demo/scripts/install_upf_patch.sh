#!/usr/bin/env bash
#
# install_mec_patch.sh
#
# Purpose: Script to be run on MEC PC.
#          Applies a patch to SRK's docker-compose.override.yaml file
#          to allow the MEC server to have its own IP address
#          on the N6 network.
#          Copies the original compose file, checks the patch applies
#          cleanly, applies the patch, and validates the patch.
#          
#
# Prerequisites:
# - OAI/Sionna RK must already be installed.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[install-upf-patch] %s\n' "$*"
}

die() {
    printf '[install-upf-patch] ERROR: %s\n' "$*" >&2
    exit 1
}


# Get script and repo root directories
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"


# Select the Sionna-RK configuration used for Compose validation.
# Supported values: b200 or rfsim.
CONFIG_NAME="${1:-b200}"

case "${CONFIG_NAME}" in
    b200|x410|rfsim)
        ;;
    *)
        die "Unsupported configuration '${CONFIG_NAME}'. Use b200, x410, or rfsim."
        ;;
esac


# Location of patch file
PATCH_FILE="${REPO_ROOT}/vlm-demo/patches/mec-upf.patch"

# Location of compose file to patch
COMPOSE_FILE="${REPO_ROOT}/gnb/config/common/docker-compose.override.yaml"
COMPOSE_DIR="$(dirname -- "${COMPOSE_FILE}")"

# b200 or rfsim env file
ENV_FILE="${REPO_ROOT}/gnb/config/${CONFIG_NAME}/.env"

# Directory to create backup of original docker file
BACKUP_DIR="${REPO_ROOT}/original-srk-files/gnb"
BACKUP_FILE="${BACKUP_DIR}/docker-compose.override.yaml"

# Commands to add and what's expected
ROUTE_COMMAND='ip route add 192.168.72.128/26 dev eth1 src 192.168.72.134 table eth1_table'


# Check that git, docker, and docker compose are installed
command -v git >/dev/null 2>&1 ||
    die "git is not installed"

command -v docker >/dev/null 2>&1 ||
    die "docker is not installed"

docker compose version >/dev/null 2>&1 ||
    die "Docker Compose plugin is unavailable"


# Check that the patch, docker compose, and env files are present
[[ -f "${PATCH_FILE}" ]] ||
    die "Patch file not found: ${PATCH_FILE}"

[[ -f "${COMPOSE_FILE}" ]] ||
    die "Docker Compose file not found: ${COMPOSE_FILE}"

[[ -f "${ENV_FILE}" ]] ||
    die "Sionna-RK environment file not found: ${ENV_FILE}"

git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    die "Repository root is not a Git working tree: ${REPO_ROOT}"


# Make repeated installation safe.
if grep -Fq "${ROUTE_COMMAND}" "${COMPOSE_FILE}"; then
    log "UPF MEC route patch is already installed"

    if [[ -f "${BACKUP_FILE}" ]]; then
        log "Original Compose backup exists at: ${BACKUP_FILE}"
    else
        log "WARNING: Patch is installed, but no original backup was found at ${BACKUP_DIR}"
    fi

    exit 0
fi


cd "${REPO_ROOT}"


# Check if applies cleanly
log "Checking whether the patch applies cleanly"
if ! git apply --check "${PATCH_FILE}"; then
    die "Patch does not apply cleanly. No backup was created or removed."
fi


# Save the untouched Compose file only if no original backup exists.
if [[ -e "${BACKUP_FILE}" ]]; then
    log "Original Compose backup exists: ${BACKUP_FILE}
    Applying patch but not backing up compose file."
else
    mkdir -p "${BACKUP_DIR}"
    log "Saving original Docker Compose file"
    cp --preserve=mode,timestamps \
        "${COMPOSE_FILE}" \
        "${BACKUP_FILE}"

    log "Original saved to:"
    log "${BACKUP_FILE}"
fi


# Apply patch
log "Applying patch"
git apply "${PATCH_FILE}"


# Check if applies cleanly
log "Validating patched Compose configuration with '${CONFIG_NAME}'"
if ! (
    cd "${COMPOSE_DIR}"

    docker compose \
        --env-file "${ENV_FILE}" \
        -f docker-compose.yaml \
        -f docker-compose.override.yaml \
        config --quiet
); then
    log "Compose validation failed; reversing the patch"

    cd "${REPO_ROOT}"
    git apply --reverse "${PATCH_FILE}" || true

    die "Patch was reversed. The original backup remains at ${BACKUP_FILE}."
fi


log "Patch installed successfully"
log "Modified file: ${COMPOSE_FILE}"
log "Validation environment: ${ENV_FILE}"
log "Original backup: ${BACKUP_FILE}"
log "The new route will take effect the next time oai-upf is created."
