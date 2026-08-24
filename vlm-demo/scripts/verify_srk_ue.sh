#!/usr/bin/env bash
#
# verify_srk_ue.sh
#
# Purpose: Script to be run on UE PC.
#          Determines if docker and docker compose are installed, if
#          required OAI/Sionna RK docker image has been
#          built, and if necessary files are present.
#
# Prerequisites:
# - OAI/Sionna RK must already be installed.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[verify-srk-ue] %s\n' "$*"
}

die() {
    printf '[verify-srk-ue] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --ue-image IMAGE        UE image to verify
  --compose-file FILE     Path to Docker Compose file to verify
  --config-dir DIR        Path to configuration directory to verify
  -h, --help              Show this help message
EOF
}

# Requires value if flag is present
require_value() {
    [[ $# -ge 2 && -n "$2" ]] ||
        die "Option $1 requires a value"
}


# Set OAI/SRK UE image to verify
UE_IMAGE="${UE_IMAGE:-oai-nr-ue-cuda:latest}"

# Get script and repo root directories
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

# Set files to verify
COMPOSE_FILE="${REPO_ROOT}/ue/config/common/docker-compose.yaml"
CONFIG_DIR="${REPO_ROOT}/ue/config"


# Parse command lines
while (( $# > 0 )); do
    case "$1" in
        --ue-image)
            require_value "$@"
            UE_IMAGE="$2"
            shift 2
            ;;
        --ue-image=*)
            UE_IMAGE="${1#*=}"
            [[ -n "$UE_IMAGE" ]] ||
                die "Option --ue-image requires a value"
            shift
            ;;
        --compose-file)
            require_value "$@"
            COMPOSE_FILE="$2"
            shift 2
            ;;
        --compose-file=*)
            COMPOSE_FILE="${1#*=}"
            [[ -n "$COMPOSE_FILE" ]] ||
                die "Option --compose-file requires a value"
            shift
            ;;
        --config-dir)
            require_value "$@"
            CONFIG_DIR="$2"
            shift 2
            ;;
        --config-dir=*)
            CONFIG_DIR="${1#*=}"
            [[ -n "$CONFIG_DIR" ]] ||
                die "Option --config-dir requires a value"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "Unknown option: $1"
            ;;
        *)
            die "Unexpected positional argument: $1"
            ;;
    esac
done

(( $# == 0 )) ||
    die "Unexpected positional argument: $1"


# Check that docker is in path and that docker compose is available.
command -v docker >/dev/null 2>&1 ||
    die "Docker is not installed or not in PATH."
log "Docker found in PATH."

docker compose version >/dev/null 2>&1 ||
    die "Docker Compose is not installed or unavailable."
log "Docker compose available."


# Check that local image exists
if docker image inspect "$UE_IMAGE" >/dev/null 2>&1; then
    log "Docker image found: $UE_IMAGE"
else
    die "Docker image not found locally: $UE_IMAGE"
fi

# Check that files exist.
[[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"
log "Found Sionna RK Docker compose file."
[[ -d "$CONFIG_DIR" ]] || die "Config directory not found: $CONFIG_DIR"
log "Found Sionna RK config directory. Searching for a .env file..."
find "$CONFIG_DIR" -mindepth 2 -maxdepth 2 -type f -name '.env' -print -quit | grep -q . ||
    die "No $CONFIG_DIR/{directory}/.env file found."
log "Found one Sionna RK .env file."


# End sequence
log "Sionna RK UE image and files located."
