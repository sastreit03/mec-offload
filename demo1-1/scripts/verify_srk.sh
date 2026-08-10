#!/usr/bin/env bash
#
# verify_srk.sh
#
# Purpose: Script to be run on MEC PC.
#          Determines if docker and docker compose are installed, if
#          required OAI/Sionna RK docker images have been 
#          built, and if necessary files are present.
#
# Prerequisites:
# - OAI/Sionna RK must already be installed.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[verify-srk] %s\n' "$*"
}

die() {
    printf '[verify-srk] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --gnb-image IMAGE       gNB image to verify
  --upf-image IMAGE       UPF image to verify
  --ext-dn-image IMAGE    External data-network image to verify
  --compose-file FILE     Docker Compose file to verify
  --config-dir DIR        Configuration directory to verify
  -h, --help              Show this help message
EOF
}

require_value() {
    [[ $# -ge 2 && -n "$2" ]] ||
        die "Option $1 requires a value"
}


# Set OAI/SRK images to verify
GNB_IMAGE="${GNB_IMAGE:-oai-gnb-cuda:latest}"
UPF_IMAGE="${UPF_IMAGE:-oaisoftwarealliance/oai-upf:v2.1.10}"
EXT_DN_IMAGE="${EXT_DN_IMAGE:-oaisoftwarealliance/trf-gen-cn5g:latest}"

# Get script and repo root directories
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

# Set files to verify
COMPOSE_FILE="${REPO_ROOT}/gnb/config/common/docker-compose.yaml"
CONFIG_DIR="${REPO_ROOT}/gnb/config"


# Parse command lines
while (( $# > 0 )); do
    case "$1" in
        --gnb-image)
            require_value "$@"
            GNB_IMAGE="$2"
            shift 2
            ;;
        --gnb-image=*)
            GNB_IMAGE="${1#*=}"
            [[ -n "$GNB_IMAGE" ]] ||
                die "Option --gnb-image requires a value"
            shift
            ;;
        --upf-image)
            require_value "$@"
            UPF_IMAGE="$2"
            shift 2
            ;;
        --upf-image=*)
            UPF_IMAGE="${1#*=}"
            [[ -n "$UPF_IMAGE" ]] ||
                die "Option --upf-image requires a value"
            shift
            ;;
        --ext-dn-image)
            require_value "$@"
            EXT_DN_IMAGE="$2"
            shift 2
            ;;
        --ext-dn-image=*)
            EXT_DN_IMAGE="${1#*=}"
            [[ -n "$EXT_DN_IMAGE" ]] ||
                die "Option --ext-dn-image requires a value"
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


# Make array of docker image names
IMAGES=(
    "$GNB_IMAGE"
    "$UPF_IMAGE"
    "$EXT_DN_IMAGE"
)


# Check that docker is in path and that docker compose is available.
command -v docker >/dev/null 2>&1 ||
    die "Docker is not installed or not in PATH."
log "Docker found in PATH."

docker compose version >/dev/null 2>&1 ||
    die "Docker Compose is not installed or unavailable."
log "Docker compose available."


# Check that local images exist
for image in "${IMAGES[@]}"; do
    if docker image inspect "$image" >/dev/null 2>&1; then
        log "Docker image found: $image"
    else
        die "Docker image not found locally: $image"
    fi
done


# Check that files exist.
[[ -f "$COMPOSE_FILE" ]] || die "Compose file not found: $COMPOSE_FILE"
log "Found Sionna RK Docker compose file."
[[ -d "$CONFIG_DIR" ]] || die "Config directory not found: $CONFIG_DIR"
log "Found Sionna RK config directory. Searching for a .env file..."
find "$CONFIG_DIR" -mindepth 2 -maxdepth 2 -type f -name '.env' -print -quit | grep -q . ||
    die "No $CONFIG_DIR/{directory}/.env file found."
log "Found one Sionra RK .env file."


# End sequence
log "All required Sionna RK images and files located."