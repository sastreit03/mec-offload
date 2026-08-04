#!/bin/bash
#
# stop_mec.sh
#
# Purpose: Remove the MEC server docker container.
#
# Prerequisites:
# - MEC server container should be running before removing it.
# - Every time the OAI 5GC containers are removed, the MEC container
#   must be restarted, too.

set -e  # Stop script on any error

# Remove docker container
echo "Removing mec-yolo container."
docker rm -f mec-yolo