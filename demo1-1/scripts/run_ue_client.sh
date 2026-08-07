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

set -e  # Stop script on any error

# UE image and container names
UE_IMAGE="${UE_IMAGE:-oai-nr-ue-cuda-mec:demo1-1}"
UE_CONTAINER=oai-nr-ue

# Check that the UE container is running
[[ "$(docker inspect -f '{{.State.Running}} {{.Config.Image}}' "$UE_CONTAINER" \
  2>/dev/null)" == "true $UE_IMAGE" ]] \
  || { echo "ERROR: $UE_CONTAINER is not running from image $UE_IMAGE" >&2; exit 1; }
printf 'Found "%s" container\n' "$UE_CONTAINER"

# Get UE and MEC IP addresses
UE_TUN=$(docker exec "$UE_CONTAINER" ip -o link show | awk -F': ' '$2 ~ /^oaitun_ue/ {print $2; exit}')
[[ -n "$UE_TUN" ]] || { echo "ERROR: no oaitun_ue interface found in $UE_CONTAINER" >&2; exit 1; }

UE_IP=$(docker exec "$UE_CONTAINER" ip -4 -o addr show dev "$UE_TUN" | awk '{print $4}' | cut -d/ -f1 | head -n1)
[[ -n "$UE_IP" ]] || { echo "ERROR: no IPv4 address found for $UE_TUN in $UE_CONTAINER" >&2; exit 1; }

MEC_IP=192.168.72.136

# Check that python dependencies are present.
docker exec "$UE_CONTAINER" /opt/ueclientvenv/bin/python -c 'from importlib.metadata import version;
from pip._vendor.packaging.version import Version as V;
assert V("2.32") <= V(version("requests")) < V("3");
assert V("4.10") <= V(version("opencv-python-headless")) < V("5")'
printf 'Python dependencies are present\n'

# Check that the test image is present.
[[ -f ../ue-client/coco_test.jpg ]] || { echo "ERROR: ../ue-client/coco_test.jpg not found" >&2; exit 1; }
printf 'Found test image\n\n'

# Copy files from current directory to the UE docker container
docker cp ../ue-client/ue_client.py "$UE_CONTAINER":/tmp/ue_client.py
docker cp ../ue-client/coco_test.jpg "$UE_CONTAINER":/tmp/coco_test.jpg

# Run inference on MEC server
printf '\nRunning request for inference from MEC server\n'
docker exec "$UE_CONTAINER" \
  /opt/ueclientvenv/bin/python \
  /tmp/ue_client.py \
  --server "http://$MEC_IP:8080" \
  --source-ip "$UE_IP" \
  --image /tmp/coco_test.jpg \
  --output /tmp/annotated.jpg \
  --conf 0.25 \
  --iou 0.70 \
  --imgsz 640 \
  --count 1 \
  --result-json /tmp/mec-inference-result.json

# Copy resulting json and annotated image from UE docker container
# to current directory
docker cp "$UE_CONTAINER":/tmp/annotated.jpg ../ue-client/
docker cp "$UE_CONTAINER":/tmp/mec-inference-result.json ../ue-client/
