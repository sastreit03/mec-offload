#!/bin/bash
#
# run_ue_client.sh
#
# Purpose: Script to be run on UE.
#          Uploads image to MEC server for inference, receives metadata
#          results, and annotates the original image. 
#
# Prerequisites:
# - Script must be run in directory mec-offload-ad/demo1-1/scripts.
# - The following docker containers must be running:
#    - Unmodified 5GC and gNB containers.
#    - Modified UE container.
#    - MEC server container.
# - The image coco_test.jpg must be in the directory ../ue-client/
#
# Acknowledgement: Commands below were written by Generative AI.

set -Eeuo pipefail

# Helper functions
log() {
    printf '[run-ue-client] %s\n' "$*"
}

die() {
    printf '[run-ue-client] ERROR: %s\n' "$*" >&2
    exit 1
}


# Parameters
UE_CONTAINER="${UE_CONTAINER:-oai-nr-ue}"
MEC_IP="${MEC_IP:-192.168.72.136}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
RESULTS_DIR="${DEMO_DIR}/inference-results"
CLIENT_DIR="${DEMO_DIR}/ue-client"

IMAGE_FILE_NAME="${IMAGE_FILE_NAME:-coco_test.jpg}"
IMAGE_FILE_PATH="$CLIENT_DIR/$IMAGE_FILE_NAME"
CLIENT_FILE_NAME="${CLIENT_FILE_NAME:-ue_client.py}"
CLIENT_FILE_PATH="$CLIENT_DIR/$CLIENT_FILE_NAME"

RUN_ID=$(date +%Y%m%d-%H%M%S)
ANNOTATED_FILE_NAME="${RUN_ID}_annotated.jpg"
JSON_FILE_NAME="${RUN_ID}_inference_result.json"


# Make the results directory if it does not exist
mkdir -p "$RESULTS_DIR" || die "Unable to create results directory: $RESULTS_DIR"


# Check that UE container is running
log "Checking that UE container is running..."
[[ "$(docker inspect -f '{{.State.Running}}' "$UE_CONTAINER" 2>/dev/null)" == "true" ]] ||
    die "Container is missing or not running: $UE_CONTAINER"



# Get tunnel interface name and IP address
UE_TUN=$(docker exec "$UE_CONTAINER" ip -o link show | awk -F': ' '$2 ~ /^oaitun_ue/ {print $2; exit}')
[[ -n "$UE_TUN" ]] || die "no oaitun_ue interface found in $UE_CONTAINER"

UE_IP=$(docker exec "$UE_CONTAINER" ip -4 -o addr show dev "$UE_TUN" | awk '{print $4}' | cut -d/ -f1 | head -n1)
[[ -n "$UE_IP" ]] || die "no IPv4 address found for $UE_TUN in $UE_CONTAINER"

log "UE interface and IP address:"
log "UE_TUN=$UE_TUN"
log "UE_IP=$UE_IP"


# Check that python dependencies are present.
docker exec "$UE_CONTAINER" /opt/ueclientvenv/bin/python -c 'from importlib.metadata import version;
from pip._vendor.packaging.version import Version as V;
assert V("2.32") <= V(version("requests")) < V("3");
assert V("4.10") <= V(version("opencv-python-headless")) < V("5")' ||
    die "Python dependencies could not be verified."
log "Python dependencies are present."


# Check that the files are present.
[[ -f "$IMAGE_FILE_PATH" ]] || die "Image not found: $IMAGE_FILE_PATH"
log "Found test image: $IMAGE_FILE_PATH"
[[ -f "$CLIENT_FILE_PATH" ]] || die "File not found: $CLIENT_FILE_PATH"
log "Found Python script: $CLIENT_FILE_PATH"

# Copy files from current directory to the UE docker container
log "Copying files to $UE_CONTAINER..."
docker cp "$CLIENT_FILE_PATH" "$UE_CONTAINER:/tmp/$CLIENT_FILE_NAME" ||
    die "Unable to copy $CLIENT_FILE_PATH to $UE_CONTAINER."
docker cp "$IMAGE_FILE_PATH" "$UE_CONTAINER:/tmp/$IMAGE_FILE_NAME" ||
    die "Unable to copy $IMAGE_FILE_PATH to $UE_CONTAINER."


# Run inference on MEC server
log "Running request for inference from MEC server ($MEC_IP)..."
docker exec "$UE_CONTAINER" \
  /opt/ueclientvenv/bin/python \
  /tmp/$CLIENT_FILE_NAME \
  --server "http://$MEC_IP:8080" \
  --source-ip "$UE_IP" \
  --image /tmp/$IMAGE_FILE_NAME \
  --output /tmp/$ANNOTATED_FILE_NAME \
  --conf 0.25 \
  --iou 0.70 \
  --imgsz 640 \
  --count 1 \
  --result-json /tmp/$JSON_FILE_NAME ||
  die "Failure in running requested inference."
log "MEC inference completed."

# Copy resulting json and annotated image from UE docker container
# to current directory
log "Copying files from $UE_CONTAINER to $RESULTS_DIR"
docker cp "$UE_CONTAINER:/tmp/$ANNOTATED_FILE_NAME" "$RESULTS_DIR/" ||
    die "Unable to copy annotated image to $RESULTS_DIR"
docker cp "$UE_CONTAINER:/tmp/$JSON_FILE_NAME" "$RESULTS_DIR/" ||
    die "Unable to copy inference result JSON to $RESULTS_DIR"
