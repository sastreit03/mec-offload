#!/usr/bin/env bash
#
# stop_client.sh
#
# Purpose: Script to be run on UE PC.
#          Stops the MEC client on UE DGX Spark.
#
# Prerequisites:
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail  # Stop script on any error

# Helper functions
log() {
    printf '[stop-client] %s\n' "$*"
}

die() {
    printf '[stop-client] ERROR: %s\n' "$*" >&2
    exit 1
}


# Check that UE container is running
UE_CONTAINER="${UE_CONTAINER:-oai-nr-ue}"

[[ "$(docker inspect -f '{{.State.Running}}' "$UE_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $UE_CONTAINER"
log "Container is running: $UE_CONTAINER"


# Check that the application is not currently running
if docker exec "$UE_CONTAINER" \
    pgrep -f '/opt/ue-demo/app.py' >/dev/null
then
    log "Shutting down UE application gracefully..."
    docker exec "$UE_CONTAINER" pkill -TERM -f '/opt/ue-demo/app.py'

    for _ in {1..10}; do
        if ! docker exec "$UE_CONTAINER" pgrep -f '/opt/ue-demo/app.py' >/dev/null; then
            log "UE demo application stopped."
            exit 0
        fi
        sleep 1
    done

    log "Graceful stop timed out; forcing stop."

    docker exec "$UE_CONTAINER" pkill -KILL -f '/opt/ue-demo/app.py'

    log "UE demo application force-stopped."
else
    log "UE MEC client application is not running."
fi
