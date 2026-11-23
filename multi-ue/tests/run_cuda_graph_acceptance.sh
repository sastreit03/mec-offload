#!/usr/bin/env bash
# Run on DGX Spark; no radio/core containers or running MEC services are changed.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
evidence_dir=${1:?Usage: bash tests/run_cuda_graph_acceptance.sh NEW_EVIDENCE_DIRECTORY}
image_name=${MEC_GRAPH_IMAGE:-multi-ue-mec:cuda-graphs-acceptance}
mkdir -p -- "$evidence_dir"
evidence_dir=$(cd -- "$evidence_dir" && pwd)
if [[ -e "$evidence_dir/production" || -e "$evidence_dir/traced" ]]; then
    echo 'Choose a fresh evidence directory.' >&2
    exit 1
fi
args=(--rm --gpus all --shm-size=2g -e MEC_TRACE_HANDOFF=1
    -v "$repo_dir/tests:/work/multi-ue/tests:ro"
    -v "$repo_dir/weights:/weights:ro" -v "$repo_dir/datasets:/media:ro"
    -v "$evidence_dir:/evidence")
reference=()
if [[ -n ${MEC_GRAPH_REFERENCE_SOURCE:-} ]]; then
    args+=(-v "$MEC_GRAPH_REFERENCE_SOURCE:/baseline_workers.py:ro")
    reference=(--reference-source /baseline_workers.py)
fi
docker image inspect "$image_name" > "$evidence_dir/image.json"
nvidia-smi > "$evidence_dir/hardware.txt"
docker run "${args[@]}" -e MEC_TEST_CUDA=1 "$image_name" python -m pytest -q \
    tests/test_cuda_graphs.py tests/test_graph_lifetime.py tests/test_rgb_preparation.py tests/test_shared_rgb.py \
    tests/test_frame_handoff.py tests/test_queue_timing.py 2>&1 | tee "$evidence_dir/tests.log"
docker run "${args[@]}" "$image_name" python tests/run_production_handoff_trial.py \
    --shared-rgb --media /media/profile.mov --weights /weights --output /evidence \
    --run-id production "${reference[@]}" 2>&1 | tee "$evidence_dir/production.log"
docker run "${args[@]}" "$image_name" nsys profile --sample=none --cpuctxsw=none \
    --trace=cuda,nvtx --cuda-graph-trace=node -o /evidence/graphs_trace \
    python tests/run_production_handoff_trial.py --shared-rgb --media /media/profile.mov \
    --weights /weights --output /evidence --run-id traced 2>&1 | tee "$evidence_dir/trace.log"
docker run --rm -v "$evidence_dir:/evidence" "$image_name" nsys export --type sqlite \
    --output /evidence/graphs_trace.sqlite /evidence/graphs_trace.nsys-rep \
    > "$evidence_dir/export.log" 2>&1
python3 "$repo_dir/tests/report_cuda_graph_trace.py" "$evidence_dir/graphs_trace.sqlite" \
    "$evidence_dir/traced/mec/trial-summary.json" --output "$evidence_dir/trace-report.json"
python3 "$repo_dir/tests/report_graph_latency.py" "$evidence_dir/production/mec/jobs.jsonl" \
    > "$evidence_dir/latency-report.json"
