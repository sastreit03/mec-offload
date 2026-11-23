#!/usr/bin/env bash
# Run on the DGX Spark. Supply a fresh evidence directory as the first argument.
set -euo pipefail
test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
evidence_dir=${1:?Usage: bash run_shared_memory_probe.sh NEW_EVIDENCE_DIRECTORY}
image_name=${MEC_PROBE_IMAGE:-multi-ue-mec:queue-acceptance}
mkdir -p -- "$evidence_dir"
evidence_dir=$(cd -- "$evidence_dir" && pwd)
if [[ -e "$evidence_dir/plain" || -e "$evidence_dir/traced-nodes" ]]; then
    echo 'Choose a fresh directory to preserve earlier results.' >&2
    exit 1
fi
docker image inspect "$image_name" > "$evidence_dir/image.json"
nvidia-smi > "$evidence_dir/nvidia-smi.txt"
docker run --rm --gpus all --ipc=host \
    -v "$test_dir:/tests:ro" -v "$evidence_dir:/evidence" "$image_name" \
    python /tests/probe_shared_memory_cuda.py --evidence /evidence/plain \
    2>&1 | tee "$evidence_dir/plain.log"
docker run --rm --gpus all --ipc=host \
    -v "$test_dir:/tests:ro" -v "$evidence_dir:/evidence" "$image_name" \
    nsys profile --trace=cuda,nvtx --cuda-graph-trace=node --sample=none \
    --cpuctxsw=none -o /evidence/shared_memory_nodes \
    python /tests/probe_shared_memory_cuda.py --evidence /evidence/traced-nodes \
    2>&1 | tee "$evidence_dir/traced-nodes.log"
docker run --rm -v "$evidence_dir:/evidence" "$image_name" \
    nsys export --type sqlite --output /evidence/shared_memory_nodes.sqlite \
    /evidence/shared_memory_nodes.nsys-rep > "$evidence_dir/export.log" 2>&1
python3 "$test_dir/report_shared_memory_trace.py" \
    "$evidence_dir/shared_memory_nodes.sqlite" "$evidence_dir/traced-nodes/summary.json" \
    --output "$evidence_dir/trace-report.json"
