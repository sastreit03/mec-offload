#!/usr/bin/env bash
# throughput_test.sh
#
# Run iperf3 throughput tests from the DGX Spark (oai-ext-dn container) to a UE
# that is running an iperf3 server inside the UE container (oai-nr-ue).
#
# Required: UE IP (e.g., 12.1.1.2)
# Optional: -udp (default: TCP)
#
# Notes:
#  - CN/DN IP is assumed fixed at 192.168.72.135
#  - This script does NOT start the iperf3 server on the UE; it prints a reminder.
#  - Logs are written to ./throughput_tests/

set -euo pipefail

CN_IP="192.168.72.135"
DN_CONTAINER="oai-ext-dn"
OUT_DIR="./throughput_tests"
DURATION=30
INTERVAL=1

usage() {
  cat <<EOF
Usage: $(basename "$0") [-udp] <UE_IP>

Examples:
  $(basename "$0") 12.1.1.2
  $(basename "$0") -udp 12.1.1.2

Behavior:
  - Runs DOWNLINK then UPLINK tests from ${DN_CONTAINER} (DGX Spark) to UE iperf3 server.
  - Writes JSON output files under ${OUT_DIR}/
  - CN IP is fixed at ${CN_IP}
EOF
}

UDP=0
if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

# Minimal flag parsing
while [[ $# -gt 0 ]]; do
  case "$1" in
    -udp)
      UDP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "ERROR: Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      UE_IP="$1"
      shift
      ;;
  esac
done

if [[ -z "${UE_IP:-}" ]]; then
  echo "ERROR: UE_IP is required." >&2
  usage
  exit 2
fi

mkdir -p "$OUT_DIR"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
PROTO="tcp"
if [[ "$UDP" -eq 1 ]]; then
  PROTO="udp"
fi

echo "========================================================================"
echo "Throughput test starting (protocol=${PROTO^^})"
echo "  UE_IP       : ${UE_IP}"
echo "  CN/DN IP    : ${CN_IP} (fixed)"
echo "  Container   : ${DN_CONTAINER}"
echo "  Duration    : ${DURATION}s"
echo "  Interval    : ${INTERVAL}s"
echo
echo "NOTE: Ensure an iperf3 server is running on the UE host/container, e.g.:"
echo "  docker exec -it oai-nr-ue iperf3 -s"
echo "========================================================================"
echo

# Build iperf3 base args (client runs in oai-ext-dn on DGX Spark)
BASE_ARGS=(-t "$DURATION" -i "$INTERVAL" -B "$CN_IP" -c "$UE_IP" -J)

# DOWNLINK: DN -> UE (server on UE receives), so NO -R
DL_OUT="${OUT_DIR}/downlink_${PROTO}_${UE_IP}_${TS}.json"

# UPLINK: UE -> DN (server on UE sends), so client uses -R
UL_OUT="${OUT_DIR}/uplink_${PROTO}_${UE_IP}_${TS}.json"

run_cmd() {
  local -a cmd=("$@")
  echo "+ ${cmd[*]}"
  "${cmd[@]}"
}

if [[ "$UDP" -eq 0 ]]; then
  # TCP
  echo "[1/2] Running DOWNLINK TCP test -> ${DL_OUT}"
  run_cmd docker exec -it "$DN_CONTAINER" iperf3 "${BASE_ARGS[@]}" > "$DL_OUT"

  echo "[2/2] Running UPLINK TCP test -> ${UL_OUT}"
  run_cmd docker exec -it "$DN_CONTAINER" iperf3 "${BASE_ARGS[@]}" -R > "$UL_OUT"

else
  # UDP (saturated)
  # NOTE: -b 0 == "as fast as possible" (stress test / capacity upper bound)
  UDP_ARGS=(-u -b 0)

  echo "[1/2] Running DOWNLINK UDP test (-b 0) -> ${DL_OUT}"
  run_cmd docker exec -it "$DN_CONTAINER" iperf3 "${UDP_ARGS[@]}" "${BASE_ARGS[@]}" > "$DL_OUT"

  echo "[2/2] Running UPLINK UDP test (-b 0, reverse) -> ${UL_OUT}"
  run_cmd docker exec -it "$DN_CONTAINER" iperf3 "${UDP_ARGS[@]}" "${BASE_ARGS[@]}" -R > "$UL_OUT"
fi

echo
echo "Done."
echo "  DOWNLINK: ${DL_OUT}"
echo "  UPLINK  : ${UL_OUT}"

