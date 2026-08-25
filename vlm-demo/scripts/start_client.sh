#!/bin/bash
#
# start_client.sh
#
# Purpose: Script to be run on UE PC.
#          Runs the MEC client on UE DGX Spark. Run on separate terminal.
#
# Prerequisites:
# - The oai-nr-ue container must be running, and the MEC client application
#   must not be running.
# - Every time the oai-nr-ue container is removed, this script
#   must be re-run, too.
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail  # Stop script on any error

# Helper functions
log() {
    printf '[start-client] %s\n' "$*"
}

die() {
    printf '[start-client] ERROR: %s\n' "$*" >&2
    exit 1
}


# Check that UPF and gNB containers are running
UE_CONTAINER="${UE_CONTAINER:-oai-nr-ue}"

[[ "$(docker inspect -f '{{.State.Running}}' "$UE_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $UE_CONTAINER"
log "Container is running: $UE_CONTAINER"


# Check that the application is not currently running
if docker exec "$UE_CONTAINER" \
    pgrep -f '/opt/ue-demo/app.py' >/dev/null
then
    die "UE demo application is already running."
fi


# Run client application
docker exec \
    --workdir /opt/ue-demo \
    "$UE_CONTAINER" \
    /opt/ue-demo-venv/bin/python \
    /opt/ue-demo/app.py \
    --config /opt/ue-demo/config.yaml

echo "UE demo application started."
