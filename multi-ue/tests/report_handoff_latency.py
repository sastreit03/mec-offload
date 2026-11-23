"""Break down jobs.jsonl host timestamps; stage percentiles are not additive."""
import argparse
import json
from collections import defaultdict


STAGES = {
    "handoff_ms": ("decoded_monotonic_ns", "decoded_handler_monotonic_ns"),
    "engine_ms": ("decoded_handler_monotonic_ns", "worker_submitted_monotonic_ns"),
    "ipc_and_launcher_wait_ms": ("worker_submitted_monotonic_ns", "worker_start_monotonic_ns"),
    "worker_pre_model_ms": ("worker_start_monotonic_ns", "gpu_submit_ns"),
    "total_ms": ("decoded_monotonic_ns", "gpu_submit_ns"),
}

TRACE_STAGES = {
    "put_to_serialize": ("ipc_put_begin", "ipc_serialize_begin"),
    "serialize": ("ipc_serialize_begin", "ipc_serialize_end"),
    "pipe_write": ("ipc_write_begin", "ipc_write_end"),
    "get": ("worker_get_begin", "worker_get_end"),
    "pipe_read": ("ipc_read_begin", "ipc_read_end"),
    "deserialize": ("ipc_deserialize_begin", "ipc_deserialize_end"),
    **{name: ("launch_" + name + "_begin", "launch_" + name + "_end")
       for name in ("pin", "upload", "preprocess", "model_call", "output_ops", "output_alloc", "readback")},
    "launch": ("launch_enter", "launch_exit"),
}


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def report(path):
    groups = defaultdict(list)
    worker_jobs = defaultdict(list)
    traces = defaultdict(lambda: defaultdict(list))
    missing = 0
    sender_trace_missing = 0
    with open(path) as source:
        for line in source:
            job = json.loads(line)
            times = job.get("stage_times", {})
            sender_trace_missing += times.get("ipc_sender_trace_available") is False
            for name, (start, end) in TRACE_STAGES.items():
                if start + "_ns" not in times or end + "_ns" not in times:
                    continue
                wall = (times[end + "_ns"] - times[start + "_ns"]) / 1e6
                for group in ("all", job["application"]):
                    traces[group][name + "_wall_ms"].append(wall)
                    # put and serialization run on different threads: their
                    # thread CPU clocks must never be subtracted.
                    if name != "put_to_serialize" and start + "_thread_cpu_ns" in times and end + "_thread_cpu_ns" in times:
                        cpu = (times[end + "_thread_cpu_ns"] - times[start + "_thread_cpu_ns"]) / 1e6
                        traces[group][name + "_cpu_ms"].append(cpu)
                        traces[group][name + "_nonexecuting_ms"].append(wall - cpu)
            if any(key not in times for pair in STAGES.values() for key in pair):
                missing += 1
                continue
            row = {name: (times[end] - times[start]) / 1e6
                   for name, (start, end) in STAGES.items()}
            row.update(application=job["application"], video_id=job["video_id"],
                       frame_id=job["frame_id"])
            groups["all"].append(row)
            groups[job["application"]].append(row)
            worker_jobs[job["application"]].append((times, row))
    # The launcher cannot dequeue B until launch(A) returns. Attribute only
    # the portion of B's interval overlapping that previous launch call.
    for jobs in worker_jobs.values():
        jobs.sort(key=lambda item: item[0]["worker_start_monotonic_ns"])
        for (prior, _), (times, row) in zip(jobs, jobs[1:]):
            if "gpu_launch_end_ns" in prior:
                start = times["worker_submitted_monotonic_ns"]
                end = times["worker_start_monotonic_ns"]
                overlap = max(0, min(end, prior["gpu_launch_end_ns"]) -
                              max(start, prior["worker_start_monotonic_ns"])) / 1e6
                row["overlap_with_previous_launch_ms"] = overlap
                row["remaining_ipc_interval_ms"] = row["ipc_and_launcher_wait_ms"] - overlap
            if all(k in times for k in ("ipc_write_begin_ns", "ipc_write_end_ns")) and all(
                    k in prior for k in ("launch_enter_ns", "launch_exit_ns", "launch_model_call_begin_ns", "launch_model_call_end_ns")):
                write_start, write_end = times["ipc_write_begin_ns"], times["ipc_write_end_ns"]
                row["pipe_write_ms"] = (write_end - write_start) / 1e6
                for label, start, end in (
                    ("previous_launch", "launch_enter_ns", "launch_exit_ns"),
                    ("previous_model_call", "launch_model_call_begin_ns", "launch_model_call_end_ns"),
                ):
                    row["pipe_write_overlap_" + label + "_ms"] = max(
                        0, min(write_end, prior[end]) - max(write_start, prior[start])) / 1e6
    summary = {}
    for name, rows in groups.items():
        summary[name] = dict(count=len(rows), stages={
            stage: {label: round(percentile([r[stage] for r in rows], fraction), 4)
                    for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99))}
            for stage in STAGES})
    trace_summary = {group: {name: dict(count=len(values), **{
        label: round(percentile(values, fraction), 4)
        for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99))})
        for name, values in stages.items()} for group, stages in traces.items()}
    return dict(missing_timestamps=missing, sender_trace_missing=sender_trace_missing,
                summary=summary, trace_summary=trace_summary,
                slowest_frames=sorted(groups["all"], key=lambda r: r["total_ms"], reverse=True)[:8],
                note="IPC interval includes feeder serialization, transfer, and waiting for the single application launcher. Endpoint is before the model call, not hardware start. Trace wall minus CPU time includes blocking and scheduling together; small negative differences reflect clock sampling noise. A blocking get/read can start before the frame exists. Intervals overlap and must not be added.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs")
    args = parser.parse_args()
    print(json.dumps(report(args.jobs), indent=2))
