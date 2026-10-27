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

   # Aetheredge uses one root-owned multi-user server for the root MEC container
   # and the host-UID gNB container. Two per-UID servers cannot run together.
   sudo install -d -m 1777 "$mps_pipe" "$mps_log"

   # Bound control requests so a stale daemon cannot hang this terminal.
   mps_control() {
     timeout 5s sudo env \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control
   }

   daemon_args="$(pgrep -u 0 -af '[n]vidia-cuda-mps-control' || true)"
   if [[ -n "$daemon_args" && "$daemon_args" != *-multiuser-server* ]]; then
     echo "MPS is already running without multi-user mode; stop its CUDA clients and restart MPS" >&2
     exit 1
   fi

   if [[ -z "$daemon_args" ]]; then
     timeout 5s sudo env \
       CUDA_VISIBLE_DEVICES=0 \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control -d -multiuser-server
   fi

   printf 'set_default_active_thread_percentage %s\n' "$mps_pct" | mps_control

   server_list="$(printf 'get_server_list\n' | mps_control)"
   if [[ -z "${server_list//[[:space:]]/}" ]]; then
     # Prestart the root server used by the MEC; multi-user mode also accepts
     # CUDA clients launched by the host-UID gNB container.
     printf 'start_server -uid 0\n' | mps_control
     server_list="$(printf 'get_server_list\n' | mps_control)"
   fi
   if [[ -z "${server_list//[[:space:]]/}" ]]; then
     echo "MPS control daemon started, but no CUDA server is available" >&2
     exit 1
   fi
   echo "MPS server ready: $server_list"
   ;;
 *) printf 'Usage: %s {build-mec|build-edge|start-mec|stop-mec|start-edge|stop-edge|gpu|mps-start}\n' "$0"; exit 2 ;;
esac
