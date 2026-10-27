#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# These commands manage the MEC additions; the core and RAN stacks have separate launchers.
case "${1:-}" in
 build-mec) docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:baseline . ;;
 build-edgeric) docker build -f mec/docker/Dockerfile.edgeric -t multi-ue-edgeric:baseline .. ;;
 start-mec) mkdir -p runs weights; docker compose -f mec/docker/compose.mec.yml up -d ;;
 stop-mec) docker compose -f mec/docker/compose.mec.yml down ;;
 start-edgeric) mkdir -p runs "${RAN_TRACE_DIR:-../edgeric-validation}"; docker compose -f mec/docker/compose.edgeric.yml up -d ;;
 stop-edgeric) docker compose -f mec/docker/compose.edgeric.yml down ;;
 gpu) docker run --rm --gpus all multi-ue-mec:baseline python -c 'import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name())' ;;
 start-mps)
   mps_pipe="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
   mps_log="${CUDA_MPS_LOG_DIRECTORY:-/tmp/nvidia-log}"
   mps_pct="${MPS_ACTIVE_THREAD_PCT:-30.0}"

   # The MEC container runs as UID 0, so this machine needs one UID 0 server.
   # The gNB runs on a different machine with its own independent MPS daemon.
   sudo -v
   sudo -n install -d -m 1777 "$mps_pipe" "$mps_log"

   # Bound control requests so a stale daemon cannot hang this terminal.
   mps_control() {
     timeout 5s sudo -n env \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control
   }

   daemon_args="$(pgrep -af '[n]vidia-cuda-mps-control' || true)"
   root_daemon_args="$(pgrep -u 0 -af '[n]vidia-cuda-mps-control' || true)"
   if [[ -n "$daemon_args" && -z "$root_daemon_args" ]]; then
     echo "MPS is already running as a non-root user; stop it before starting the MEC MPS service" >&2
     exit 1
   fi

   # Aetheredge: a process can survive after its control socket disappears.
   # With no active MEC, remove that stale state before launching a new daemon.
   control_ready=false
   if [[ -e "$mps_pipe/control" ]] && \
      printf 'get_server_list\n' | mps_control >/dev/null 2>&1; then
     control_ready=true
   fi
   if [[ "$control_ready" != true ]]; then
     if [[ "$(docker inspect -f '{{.State.Running}}' multi-ue-mec 2>/dev/null || true)" == "true" ]]; then
       echo "MPS control is unavailable while the MEC is running; stop the MEC before repairing MPS" >&2
       exit 1
     fi
     sudo -n pkill -TERM -f '[n]vidia-cuda-mps-server' 2>/dev/null || true
     sudo -n pkill -TERM -f '[n]vidia-cuda-mps-control' 2>/dev/null || true
     sleep 1
     sudo -n pkill -KILL -f '[n]vidia-cuda-mps-server' 2>/dev/null || true
     sudo -n pkill -KILL -f '[n]vidia-cuda-mps-control' 2>/dev/null || true
     sudo -n rm -rf "$mps_pipe"
     sudo -n install -d -m 1777 "$mps_pipe" "$mps_log"
     timeout 5s sudo -n env \
       CUDA_VISIBLE_DEVICES=0 \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control -d
   fi

   printf 'set_default_active_thread_percentage %s\n' "$mps_pct" | mps_control

   server_list="$(printf 'get_server_list\n' | mps_control)"
   if [[ -z "${server_list//[[:space:]]/}" ]]; then
     # Prestart the root server used by the root MEC container.
     printf 'start_server -uid 0\n' | mps_control
     server_list="$(printf 'get_server_list\n' | mps_control)"
   fi
   if [[ -z "${server_list//[[:space:]]/}" ]]; then
     echo "MPS control daemon started, but no CUDA server is available" >&2
     exit 1
   fi
   echo "MPS server ready: $server_list"
   ;;
 stop-mps)
   mps_pipe="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
   mps_log="${CUDA_MPS_LOG_DIRECTORY:-/tmp/nvidia-log}"

   # Aetheredge: do not terminate MPS while the MEC still has CUDA clients.
   if [[ "$(docker inspect -f '{{.State.Running}}' multi-ue-mec 2>/dev/null || true)" == "true" ]]; then
     echo "MEC is still running; run stop-mec before stop-mps" >&2
     exit 1
   fi
   if ! pgrep -u 0 -af '[n]vidia-cuda-mps-control' >/dev/null && \
      [[ ! -e "$mps_pipe/control" ]]; then
     echo "MPS is already stopped"
     exit 0
   fi

   # Match the root ownership and directories used by start-mps, with a bound
   # so an unhealthy control daemon cannot hang the operator terminal.
   sudo -v
   if [[ -e "$mps_pipe/control" ]]; then
     printf 'quit\n' | timeout 5s sudo -n env \
       CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
       CUDA_MPS_LOG_DIRECTORY="$mps_log" \
       nvidia-cuda-mps-control 2>/dev/null || true
   fi
   # Aetheredge: also clear a daemon whose control socket was already lost.
   sudo -n pkill -TERM -f '[n]vidia-cuda-mps-server' 2>/dev/null || true
   sudo -n pkill -TERM -f '[n]vidia-cuda-mps-control' 2>/dev/null || true
   sleep 1
   sudo -n pkill -KILL -f '[n]vidia-cuda-mps-server' 2>/dev/null || true
   sudo -n pkill -KILL -f '[n]vidia-cuda-mps-control' 2>/dev/null || true
   sudo -n rm -rf "$mps_pipe"
   if pgrep -u 0 -af '[n]vidia-cuda-mps-(control|server)' >/dev/null; then
     echo "MPS did not stop within five seconds" >&2
     exit 1
   fi
   echo "MPS stopped"
   ;;
 *) printf 'Usage: %s {build-mec|build-edgeric|start-mec|stop-mec|start-edgeric|stop-edgeric|gpu|start-mps|stop-mps}\n' "$0"; exit 2 ;;
esac
