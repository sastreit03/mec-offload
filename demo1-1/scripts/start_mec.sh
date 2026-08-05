#!/bin/bash
#
# start_mec.sh
#
# Purpose: Script to be run on MEC PC.
#          Build the MEC server docker container.
#
# Prerequisites:
# - OAI 5GC docker containers must be running.
# - Script must be run in demo1/scripts/ directory.
# - Every time the OAI 5GC containers are removed, the MEC container
#   must be restarted, too.
#
# Acknowledgement: Commands below were written by Generative AI.

set -e  # Stop script on any error

# Build docker container
docker compose --env-file ../mec-server/.env -f ../mec-server/compose.mec.yml up -d --build