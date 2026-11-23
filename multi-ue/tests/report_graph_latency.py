"""Separate first-use registration and subsequent graph submission timings."""
import argparse
import json
from pathlib import Path
from report_handoff_latency import percentile


def report(path):
    jobs=[json.loads(line) for line in Path(path).read_text().splitlines()]
    groups={'all':jobs, 'first_frame_per_receiver':[j for j in jobs if j['frame_id']==0],
            'after_first_frame':[j for j in jobs if j['frame_id']>0],
            'after_first_two_frames':[j for j in jobs if j['frame_id']>1]}
    stages={
        'decoded_to_model_submit_ms':('decoded_monotonic_ns','gpu_submit_ns'),
        'dequeue_to_launch_entry_ms':('worker_start_monotonic_ns','launch_enter_ns'),
        'model_submission_call_ms':('launch_model_call_begin_ns','launch_model_call_end_ns'),
        'adapter_launch_ms':('launch_enter_ns','launch_exit_ns'),
    }
    result={}
    for name,rows in groups.items():
        result[name]={'frames':len(rows),'stages':{}}
        for stage,(start,end) in stages.items():
            values=[(j['stage_times'][end]-j['stage_times'][start])/1e6 for j in rows
                    if start in j['stage_times'] and end in j['stage_times']]
            if values:
                result[name]['stages'][stage]={label:percentile(values,fraction)
                    for label,fraction in [('p50',.5),('p95',.95),('p99',.99),('max',1)]}
    return dict(groups=result,note='First-use region registration is inside dequeue-to-launch-entry, '
        'along with validation/mapping and scheduling; that interval is not a pure CUDA API duration. '
        'Later frames can queue behind the first. Explicit subsets are descriptive, not a replacement '
        'for all-frame latency. Host submit timestamps do not measure GPU execution start.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('jobs')
    print(json.dumps(report(parser.parse_args().jobs),indent=2))
