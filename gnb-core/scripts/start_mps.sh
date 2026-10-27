#!/usr/bin/env bash
set -euo pipefail

mps_pipe="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
mps_log="${CUDA_MPS_LOG_DIRECTORY:-/tmp/nvidia-log}"
mps_pct="${MPS_ACTIVE_THREAD_PCT:-30.0}"
host_uid="$(id -u)"

# Aetheredge: the gNB container runs as the invoking host UID. Root repairs the
# shared mount permissions, but the MPS daemon and server retain that host UID.
sudo install -d -m 1777 "$mps_pipe" "$mps_log"

# Aetheredge: prevent stale control sockets from hanging an operator terminal.
mps_control() {
    timeout 5s env \
        CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
        CUDA_MPS_LOG_DIRECTORY="$mps_log" \
        nvidia-cuda-mps-control
}

daemon_args="$(pgrep -af '[n]vidia-cuda-mps-control' || true)"
user_daemon_args="$(pgrep -u "$host_uid" -af '[n]vidia-cuda-mps-control' || true)"
if [[ -n "$daemon_args" && -z "$user_daemon_args" ]]; then
    echo "MPS is already running under another UID; stop it before starting the gNB MPS service" >&2
    exit 1
fi

if [[ -z "$user_daemon_args" ]]; then
    echo "Starting gNB MPS control daemon"
    timeout 5s env \
        CUDA_VISIBLE_DEVICES=0 \
        CUDA_MPS_PIPE_DIRECTORY="$mps_pipe" \
        CUDA_MPS_LOG_DIRECTORY="$mps_log" \
        nvidia-cuda-mps-control -d
fi

printf 'set_default_active_thread_percentage %s\n' "$mps_pct" | mps_control

server_list="$(printf 'get_server_list\n' | mps_control)"
if [[ -z "${server_list//[[:space:]]/}" ]]; then
    # Aetheredge: prestart only the same-UID server used by the gNB container.
    printf 'start_server -uid %s\n' "$host_uid" | mps_control
    server_list="$(printf 'get_server_list\n' | mps_control)"
fi
if [[ -z "${server_list//[[:space:]]/}" ]]; then
    echo "MPS control daemon started, but no gNB CUDA server is available" >&2
    exit 1
fi

echo "gNB MPS server ready: $server_list"
