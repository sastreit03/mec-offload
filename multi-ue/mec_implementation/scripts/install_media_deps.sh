#!/usr/bin/env bash
# Ubuntu 24.04: GI must match the Python interpreter's version/ABI.
# Run explicitly on the UE and MEC image/host when enabling H.264.
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends \
  python3-gi python3-venv gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
