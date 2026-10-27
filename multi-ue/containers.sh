#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# These commands manage the MEC additions; the core and RAN stacks have separate launchers.
case "${1:-}" in
 build-mec) docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:baseline . ;;
 build-edge) docker build -f mec/docker/Dockerfile.edgeric -t multi-ue-edgeric:baseline .. ;;
 start-mec) mkdir -p runs weights; docker compose -f mec/docker/compose.mec.yml up -d ;;
 stop-mec) docker compose -f mec/docker/compose.mec.yml down ;;
 start-edge) mkdir -p runs "${RAN_TRACE_DIR:-../edgeric-validation}"; docker compose -f mec/docker/compose.edgeric.yml up -d ;;
 stop-edge) docker compose -f mec/docker/compose.edgeric.yml down ;;
 gpu) docker run --rm --gpus all multi-ue-mec:baseline python -c 'import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name())' ;;
 mps-start)
   mps_pipe="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
   mps_log="${CUDA_MPS_LOG_DIRECTORY:-/tmp/nvidia-log}"
   mps_pct="${MPS_ACTIVE_THREAD_PCT:-30.0}"

   # The MEC container runs as UID 0, so its MPS server must also run as UID 0.
   sudo install -d -m 1777 "$mps_pipe" "$mps_log"

   # Do not terminate an MPS daemon that may still have active CUDA clients.
   if pgrep -f 'nvidia-cuda-mps-control -d' >/dev/null && \
      ! pgrep -u 0 -f 'nvidia-cuda-mps-control -d' >/dev/null; then
     echo "MPS is running as a non-root user; stop it before starting the MEC MPS service" >&2
     exit 1
   fi

   if ! pgrep -u 0 -f 'nvidia-cuda-mps-control -d' >/dev/null; then
     sudo env \
       CUDA_VISIBLE_DEVICES=0 \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control -d
   fi

   # Starting an existing UID-0 server is harmless and keeps repeated starts simple.
   printf 'set_default_active_thread_percentage %s\nstart_server -uid 0\n' "$mps_pct" | \
     sudo env \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control

   server_list="$(printf 'get_server_list\n' | sudo env \
     CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
     CUDA_MPS_LOG_DIRECTORY="$mps_log" \
     nvidia-cuda-mps-control)"
   if [[ -z "${server_list//[[:space:]]/}" ]]; then
     echo "MPS control daemon started, but no UID-0 server is available" >&2
     exit 1
   fi
   echo "MPS server ready: $server_list"
   ;;
 *) printf 'Usage: %s {build-mec|build-edge|start-mec|stop-mec|start-edge|stop-edge|gpu|mps-start}\n' "$0"; exit 2 ;;
esac
