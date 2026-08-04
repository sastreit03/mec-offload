#!/bin/bash
#
# run_ue_client.sh
#
# Purpose: Uploads image to MEC server for inference, receives metadata
#          results, and annotates the original image. 
#
# Prerequisites:
# - modify_oai-nr-ue.sh must be run first to modify oai-nr-ue docker container.
# - Script must be run in directory mec-offload-ad/demo1/scripts.
# - The following docker containers must be running:
#    - Unmodified 5GC and gNB containers.
#    - Modified UE container.
#    - MEC server container.
#
# Acknowledgement: Commands below were written by Generative AI.
#
# Note: this script is for initial testing only. For future sustainable
#       testing, a docker image based on the oai-nr-ue image should have
#       pip and a python virtual environment

set -e  # Stop script on any error

# Copy files from current directory to the UE docker container
docker cp ../ue-client/ue_client.py oai-nr-ue:/tmp/ue_client.py
docker cp ../ue-client/coco_test.jpg oai-nr-ue:/tmp/coco_test.jpg

# Run inference on MEC server
docker exec oai-nr-ue python3 /tmp/ue_client.py \
  --server "http://192.168.72.135:8080" \
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
