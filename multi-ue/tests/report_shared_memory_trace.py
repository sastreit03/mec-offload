"""Check an Nsight SQLite export of probe_shared_memory_cuda.py (node tracing)."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3


def report(database, summary_path):
    summary = json.loads(Path(summary_path).read_text())
    con = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    spans = con.execute("""SELECT n.start,n.end FROM NVTX_EVENTS n
        LEFT JOIN StringIds s ON n.textId=s.id
        WHERE COALESCE(n.text,s.value)='shared_memory_trial'""").fetchall()
    assert len(spans) == 1 and spans[0]["end"] is not None, "Missing trial range"
    start, end = spans[0]
    kernels = [dict(r) for r in con.execute("""SELECT k.start,k.end,k.streamId,
        k.graphNodeId,s.value AS name FROM CUPTI_ACTIVITY_KIND_KERNEL k
        JOIN StringIds s ON k.shortName=s.id WHERE k.start>=? AND k.end<=?
        ORDER BY k.start""", (start, end))]
    apis = [dict(r) for r in con.execute("""SELECT r.start,r.end,s.value AS name
        FROM CUPTI_ACTIVITY_KIND_RUNTIME r JOIN StringIds s ON r.nameId=s.id
        WHERE r.start>=? AND r.end<=? ORDER BY r.start""", (start, end))]
    copies = [dict(r) for r in con.execute("""SELECT e.name AS kind,e.label,
        COUNT(*) AS operations,SUM(m.bytes) AS bytes FROM CUPTI_ACTIVITY_KIND_MEMCPY m
        JOIN ENUM_CUDA_MEMCPY_OPER e ON m.copyKind=e.id
        WHERE m.start>=? AND m.end<=? GROUP BY e.name,e.label""", (start, end))]
    counts = Counter(k["name"] for k in kernels)
    graph_nodes = [k for k in kernels if k["graphNodeId"]]
    launches = [a for a in apis if a["name"].startswith("cudaGraphLaunch")]
    # A CPU replay call fully inside an earlier graph kernel is direct evidence
    # of asynchronous submission. GPU kernels on this stream remain ordered.
    overlaps = [dict(cpu_launch_start_ns=a["start"], cpu_launch_end_ns=a["end"],
                     running_kernel=k["name"], kernel_start_ns=k["start"], kernel_end_ns=k["end"])
                for a in launches for k in graph_nodes
                if k["start"] <= a["start"] and a["end"] <= k["end"]]
    host_uploads = [c for c in copies if c["kind"] in
                    ("CUDA_MEMCPY_KIND_HTOD", "CUDA_MEMCPY_KIND_HTOA", "CUDA_MEMCPY_KIND_UVM_HTOD")]
    frames = summary["submitted"]
    checks = dict(probe_passed=summary["status"] == "passed",
                  every_frame_verified=counts["verify_rgb"] == frames,
                  every_frame_preprocessed=counts["preprocess"] == frames,
                  every_frame_graph_replayed=len(launches) == frames,
                  graph_nodes_visible=len(graph_nodes) >= frames,
                  no_host_to_device_uploads=not host_uploads,
                  submission_during_earlier_gpu_work=bool(overlaps),
                  one_gpu_stream=len({k["streamId"] for k in kernels}) == 1)
    con.close()
    return dict(status="passed" if all(checks.values()) else "failed", checks=checks,
                trial_duration_ms=(end-start)/1e6, kernel_counts=dict(counts),
                graph_node_count=len(graph_nodes), graph_replays=len(launches),
                memory_copies=copies, host_to_device_bytes=sum(c["bytes"] for c in host_uploads),
                cpu_graph_launches_during_gpu_graph_kernel=len(overlaps), overlap_examples=overlaps,
                interpretation="Real GB10 GPU; synthetic inputs and graph. DtoH copies preserve outputs for validation. "
                "No claim about physical DRAM/cache traffic or production-model speedup.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("summary")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = report(args.database, args.summary)
    Path(args.output).write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
