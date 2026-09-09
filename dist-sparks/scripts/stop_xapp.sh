#!/usr/bin/env bash
#
# stop_xapp.sh
#
# Purpose: Script to be run on gNB PC.
#          Remove the xApp docker container.
#
# Prerequisites:
# - xApp container should be running before removing it.
# - Every time the OAI containers are removed, the xApp container
#   must be restarted, too.

set -Eeuo pipefail  # Stop script on any error

# Remove docker container
echo "Removing xApp container."
docker rm -f ran-telemetry-xapp