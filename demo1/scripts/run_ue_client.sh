#!/bin/bash
#
# run_ue_client.sh
#
# Purpose: Script to be run on UE.
#          Uploads image to MEC server for inference, receives metadata
#          results, and annotates the original image. 
#
# Prerequisites:
# - modify_oai-nr-ue.sh must be run before this script.
# - Script must be run in directory mec-offload-ad/demo1/scripts.
# - The following docker containers must be running:
#    - Unmodified 5GC and gNB containers.
#    - Modified UE container.
#    - MEC server container.
# - The image coco_test.jpg must be in the directory ../ue-client/
#
# Acknowledgement: Commands below were written by Generative AI.
#
# Note: this script is for initial testing only. For future sustainable
#       testing, a docker image based on the oai-nr-ue image should have
#       pip and a python virtual environment

set -e  # Stop script on any error

# Check that the UE image is running
UE_IMAGE="${UE_IMAGE:-oai-nr-ue-cuda:latest}"
[[ -n "$(docker ps -q --filter "ancestor=$UE_IMAGE")" ]] \
  || { echo "ERROR: no active docker container found for image $UE_IMAGE" >&2; exit 1; }
printf 'Found oai-nr-ue container\n'

# Check that python dependencies are present.
docker exec oai-nr-ue python3 -c 'from importlib.metadata import version;
from pip._vendor.packaging.version import Version as V;
assert V("2.32") <= V(version("requests")) < V("3");
assert V("4.10") <= V(version("opencv-python-headless")) < V("5")'
printf 'Python dependencies are present\n'

# Check that the test image is present.
[[ -f ../ue-client/coco_test.jpg ]] || { echo "ERROR: ../ue-client/coco_test.jpg not found" >&2; exit 1; }
printf 'Found test image\n\n'

# Copy files from current directory to the UE docker container
docker cp ../ue-client/ue_client.py oai-nr-ue:/tmp/ue_client.py
docker cp ../ue-client/coco_test.jpg oai-nr-ue:/tmp/coco_test.jpg

# Run inference on MEC server
printf '\nRunning request for inference from MEC server\n'
docker exec oai-nr-ue python3 /tmp/ue_client.py \
  --server "http://192.168.72.136:8080" \
  --source-ip "12.1.1.2" \
  --image /tmp/coco_test.jpg \
  --output /tmp/annotated.jpg \
  --conf 0.25 \
  --iou 0.70 \
  --imgsz 640 \
  --count 1 \
  --result-json /tmp/mec-inference-result.json

# Copy resulting json and annotated image from UE docker container
# to current directory
docker cp oai-nr-ue:/tmp/annotated.jpg ../ue-client/
docker cp oai-nr-ue:/tmp/mec-inference-result.json ../ue-client/
