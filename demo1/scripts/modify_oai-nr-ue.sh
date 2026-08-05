#!/bin/bash
#
# modify_oai-nr-ue.sh
#
# Purpose: Script to be run on UE.
#          Adds pip and python packages on base oai-nr-ue docker container
#          to be able to run ue_client.sh on run_ue_client.sh. This
#          script only needs to be run once when the UE docker container
#          is built.
#
# Prerequisites:
# - Must be run on the UE PC.
# - Unmodified UE docker container must be running.
#
# Acknowledgement: Commands below were written by Generative AI.

set -e  # Stop script on any error

# Download pip on UE docker container
docker exec -u 0 oai-nr-ue sh -c '
apt-get update &&
apt-get install -y --no-install-recommends python3-pip &&
rm -rf /var/lib/apt/lists/*
'

# Install python packages
docker exec -u 0 oai-nr-ue \
    python3 -m pip install --break-system-packages \
    "requests>=2.32,<3" \
    "opencv-python-headless>=4.10,<5"