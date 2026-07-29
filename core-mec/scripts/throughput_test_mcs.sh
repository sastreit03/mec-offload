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
#  - iperf3 JSON logs are written to ./throughput_tests/
#  - MCS CSV and summary logs are written to ./mcs_tests/
#  - MCS samples are collected from oai-gnb Docker logs while iperf3 runs.

set -euo pipefail

CN_IP="192.168.72.135"
DN_CONTAINER="oai-ext-dn"
GNB_CONTAINER="oai-gnb"
OUT_DIR="./throughput_tests"
MCS_OUT_DIR="./mcs_tests"
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
  - Writes MCS CSV and summary output files under ${MCS_OUT_DIR}/
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

mkdir -p "$OUT_DIR" "$MCS_OUT_DIR"

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
DL_MCS_BASE="${MCS_OUT_DIR}/downlink_${PROTO}_${UE_IP}_${TS}.mcs"

# UPLINK: UE -> DN (server on UE sends), so client uses -R
UL_OUT="${OUT_DIR}/uplink_${PROTO}_${UE_IP}_${TS}.json"
UL_MCS_BASE="${MCS_OUT_DIR}/uplink_${PROTO}_${UE_IP}_${TS}.mcs"

run_cmd() {
  local -a cmd=("$@")
  echo "+ ${cmd[*]}"
  "${cmd[@]}"
}

collect_mcs_from_logs() {
  local test_name="$1"
  local out_csv="$2"
  local start_ts="$3"
  local end_ts="$4"

  echo "timestamp,test,direction,rnti,mcs_table,mcs" > "$out_csv"

  docker logs --since "$start_ts" --until "$end_ts" --timestamps "$GNB_CONTAINER" 2>/dev/null |
    awk -v test="$test_name" '
      /dlsch_rounds|ulsch_rounds/ {
        ts = $1
        direction = /dlsch_rounds/ ? "dl" : "ul"
        rnti = ""
        mcs_table = ""
        mcs = ""

        for (i = 1; i <= NF; i++) {
          if ($i == "UE" && (i + 1) <= NF && $(i + 1) ~ /^[0-9a-fA-F]+:$/) {
            rnti = $(i + 1)
            sub(/:$/, "", rnti)
          }

          if ($i == "MCS" && (i + 2) <= NF) {
            mcs_table = $(i + 1)
            mcs = $(i + 2)
            gsub(/[()]/, "", mcs_table)
          }
        }

        if (rnti != "" && mcs ~ /^[0-9]+$/)
          print ts "," test "," direction "," rnti "," mcs_table "," mcs
      }
    ' >> "$out_csv"
}

summarize_mcs() {
  local csv="$1"
  local summary="$2"

  awk -F, '
    NR == 1 { next }

    {
      dir = $3
      mcs = $6 + 0
      n[dir]++
      sum[dir] += mcs
      sumsq[dir] += mcs * mcs

      if (n[dir] == 1 || mcs < min[dir]) min[dir] = mcs
      if (n[dir] == 1 || mcs > max[dir]) max[dir] = mcs
    }

    END {
      total = 0
      for (dir in n)
        total += n[dir]

      if (total == 0) {
        print "No MCS samples collected."
        exit
      }

      for (dir in n) {
        avg = sum[dir] / n[dir]
        var = (sumsq[dir] / n[dir]) - (avg * avg)
        if (var < 0) var = 0
        std = sqrt(var)

        printf "%s MCS: count=%d min=%d max=%d avg=%.3f stddev=%.3f\n",
               toupper(dir), n[dir], min[dir], max[dir], avg, std
      }
    }
  ' "$csv" | tee "$summary"
}

run_iperf_with_mcs() {
  local test_name="$1"
  local iperf_json="$2"
  local mcs_csv="$3"
  shift 3

  local start_ts
  start_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  echo "+ $*"
  set +e
  "$@" > "$iperf_json"
  local rc=$?
  set -e

  local end_ts
  end_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  collect_mcs_from_logs "$test_name" "$mcs_csv" "$start_ts" "$end_ts"
  summarize_mcs "$mcs_csv" "${mcs_csv%.csv}.summary.txt"

  return "$rc"
}

if [[ "$UDP" -eq 0 ]]; then
  # TCP
  echo "[1/2] Running DOWNLINK TCP test -> ${DL_OUT}"
  run_iperf_with_mcs "downlink" "$DL_OUT" "${DL_MCS_BASE}.csv" docker exec -it "$DN_CONTAINER" iperf3 "${BASE_ARGS[@]}"

  echo "[2/2] Running UPLINK TCP test -> ${UL_OUT}"
  run_iperf_with_mcs "uplink" "$UL_OUT" "${UL_MCS_BASE}.csv" docker exec -it "$DN_CONTAINER" iperf3 "${BASE_ARGS[@]}" -R

else
  # UDP (saturated)
  # NOTE: -b 0 == "as fast as possible" (stress test / capacity upper bound)
  UDP_ARGS=(-u -b 0)

  echo "[1/2] Running DOWNLINK UDP test (-b 0) -> ${DL_OUT}"
  run_iperf_with_mcs "downlink" "$DL_OUT" "${DL_MCS_BASE}.csv" docker exec -it "$DN_CONTAINER" iperf3 "${UDP_ARGS[@]}" "${BASE_ARGS[@]}"

  echo "[2/2] Running UPLINK UDP test (-b 0, reverse) -> ${UL_OUT}"
  run_iperf_with_mcs "uplink" "$UL_OUT" "${UL_MCS_BASE}.csv" docker exec -it "$DN_CONTAINER" iperf3 "${UDP_ARGS[@]}" "${BASE_ARGS[@]}" -R
fi

echo
echo "Done."
echo "  DOWNLINK: ${DL_OUT}"
echo "  DL MCS  : ${DL_MCS_BASE}.csv"
echo "  DL STATS: ${DL_MCS_BASE}.summary.txt"
echo "  UPLINK  : ${UL_OUT}"
echo "  UL MCS  : ${UL_MCS_BASE}.csv"
echo "  UL STATS: ${UL_MCS_BASE}.summary.txt"
