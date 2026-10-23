#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Reuse system GStreamer bindings while isolating the Python application dependencies.
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m grpc_tools.protoc -I contracts/proto --python_out=contracts/python/mec_contracts/generated contracts/proto/mec_edgeric.proto
printf 'Use: source scripts/environment.sh\n'
