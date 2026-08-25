#!/usr/bin/env bash
set -euo pipefail

# Run as root inside a Debian/Ubuntu-based OAI UE image, or use these packages
# while extending the OAI image in your own Dockerfile.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 \
  python3-venv \
  python3-gi \
  python3-gst-1.0 \
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

# Create python venv and install requirements
python3 -m venv --system-site-packages /opt/ue-demo-venv
/opt/ue-demo-venv/bin/python -m pip install --upgrade pip
/opt/ue-demo-venv/bin/pip install -r /opt/ue-demo/requirements.txt

# Check GStreamer packages import properly
/opt/ue-demo-venv/bin/python - <<'PY'
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)

required = [
    "filesrc",
    "decodebin",
    "queue",
    "tee",
    "videoconvert",
    "videorate",
    "videocrop",
    "videoscale",
    "x264enc",
    "h264parse",
    "rtph264pay",
    "udpsink",
    "avdec_h264",
    "jpegenc",
    "appsink",
]

missing = [name for name in required if Gst.ElementFactory.find(name) is None]
if missing:
    raise RuntimeError(f"Missing GStreamer elements: {', '.join(missing)}")

print("Python GStreamer bindings and required elements are available.")
PY
