#!/usr/bin/env bash
set -euo pipefail

# Run as root inside a Debian/Ubuntu-based OAI UE image, or use these packages
# while extending the OAI image in your own Dockerfile.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 \
  python3-venv \
  python3-gi \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  iproute2 \
  ca-certificates

python3 -m venv --system-site-packages /opt/ue-demo-venv
/opt/ue-demo-venv/bin/python -m pip install --upgrade pip
/opt/ue-demo-venv/bin/pip install -r /opt/ue-demo/requirements.txt

for element in x264enc rtph264pay h264parse avdec_h264 jpegenc appsink; do
  gst-inspect-1.0 "$element" >/dev/null
  echo "OK: GStreamer element $element"
done
