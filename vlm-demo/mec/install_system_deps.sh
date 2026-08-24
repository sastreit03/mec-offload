#!/usr/bin/env bash
set -euo pipefail

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 \
  python3-venv \
  python3-pip \
  python3-gi \
  python3-gst-1.0 \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-libav \
  ca-certificates

rm -rf /var/lib/apt/lists/*

echo "Installed GStreamer RTP/H.264 decode dependencies."
echo "Create/use a Python environment that can see python3-gi and your CUDA PyTorch installation."
