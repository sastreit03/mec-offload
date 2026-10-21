#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
 build-mec) docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:baseline . ;;
 build-edge) docker build -f mec/docker/Dockerfile.edgeric -t multi-ue-edgeric:baseline .. ;;
 start-mec) mkdir -p runs weights; docker compose -f mec/docker/compose.mec.yml up -d ;;
 stop-mec) docker compose -f mec/docker/compose.mec.yml down ;;
 start-edge) mkdir -p runs "${RAN_TRACE_DIR:-../edgeric-validation}"; docker compose -f mec/docker/compose.edgeric.yml up -d ;;
 stop-edge) docker compose -f mec/docker/compose.edgeric.yml down ;;
 gpu) docker run --rm --gpus all multi-ue-mec:baseline python -c 'import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name())' ;;
 mps-start) mkdir -p /tmp/nvidia-mps /tmp/nvidia-log; CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-log nvidia-cuda-mps-control -d ;;
 *) printf 'Usage: %s {build-mec|build-edge|start-mec|stop-mec|start-edge|stop-edge|gpu|mps-start}\n' "$0"; exit 2 ;;
esac
