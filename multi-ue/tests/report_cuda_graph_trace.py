"""Validate steady-state worker ranges in an Nsight Systems SQLite export."""
import argparse
import json
from pathlib import Path
import sqlite3


def report(database, summary_path):
    summary = json.loads(Path(summary_path).read_text())
    con = sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    ranges = con.execute("""SELECT n.start,n.end,n.globalTid FROM NVTX_EVENTS n
        LEFT JOIN StringIds s ON n.textId=s.id
        WHERE COALESCE(n.text,s.value)='mec_graph_trial'""").fetchall()
    assert len(ranges) == 3 and all(r['end'] for r in ranges)
    workers = []
    for span in ranges:
        # Nsight global IDs encode process identity above the low 24 thread bits.
        pid = span['globalTid'] & ~0xffffff
        args = (pid, span['start'], span['end'])
        kernels = [dict(r) for r in con.execute('''SELECT k.start,k.end,k.graphNodeId,
            k.correlationId,k.streamId,s.value AS name FROM CUPTI_ACTIVITY_KIND_KERNEL k
            JOIN StringIds s ON k.shortName=s.id
            WHERE k.globalPid=? AND k.start>=? AND k.end<=? ORDER BY k.start''', args)]
        apis = [dict(r) for r in con.execute('''SELECT r.start,r.end,r.correlationId,s.value AS name
            FROM CUPTI_ACTIVITY_KIND_RUNTIME r JOIN StringIds s ON r.nameId=s.id
            WHERE (r.globalTid & -16777216)=? AND r.start>=? AND r.end<=?''', args)]
        copies = [dict(r) for r in con.execute('''SELECT e.name AS kind,COUNT(*) AS operations,
            SUM(m.bytes) AS bytes FROM CUPTI_ACTIVITY_KIND_MEMCPY m
            JOIN ENUM_CUDA_MEMCPY_OPER e ON m.copyKind=e.id
            WHERE m.globalPid=? AND m.start>=? AND m.end<=? GROUP BY e.name''', args)]
        launches = [a for a in apis if a['name'].startswith('cudaGraphLaunch')]
        nodes = [k for k in kernels if k['graphNodeId']]
        overlaps = []
        graph_spans = {}
        for kernel in nodes:
            interval = graph_spans.setdefault(kernel['correlationId'], [kernel['start'], kernel['end']])
            interval[0] = min(interval[0], kernel['start'])
            interval[1] = max(interval[1], kernel['end'])
        overlapping_graphs = []
        for launch in launches:
            for correlation, (start, end) in graph_spans.items():
                if correlation != launch['correlationId'] and start <= launch['start'] < end:
                    overlapping_graphs.append(dict(cpu_launch=launch, earlier_correlation_id=correlation,
                                                   gpu_graph_start_ns=start, gpu_graph_end_ns=end))
                    break
        for launch in launches:
            match = next((k for k in nodes if k['correlationId'] != launch['correlationId']
                and max(k['start'], launch['start']) < min(k['end'], launch['end'])), None)
            if match:
                overlaps.append(dict(cpu_launch=launch, earlier_graph_kernel=match))
        workers.append(dict(global_pid=pid, kernel_count=len(kernels), graph_nodes=len(nodes),
            preprocessing_kernels=sum(k['name']=='shared_rgb_preprocess' for k in kernels),
            graph_replays=len(launches), copies=copies,
            registration_before_first_preprocessing=all(a['end'] <= min(k['start'] for k in kernels
                if k['name']=='shared_rgb_preprocess') for a in apis if a['name'].startswith('cudaHostRegister')),
            host_registration_ms=[(a['end']-a['start'])/1e6 for a in apis
                                  if a['name'].startswith('cudaHostRegister')],
            graph_launches_before_earlier_same_worker_graph_finished=len(overlapping_graphs),
            graph_interval_examples=overlapping_graphs[:3],
            graph_launches_overlapping_earlier_same_worker_kernel=len(overlaps),
            overlap_examples=overlaps[:3]))
    uploads = [c for w in workers for c in w['copies'] if c['kind'] in
        ('CUDA_MEMCPY_KIND_HTOD','CUDA_MEMCPY_KIND_HTOA','CUDA_MEMCPY_KIND_UVM_HTOD')]
    checks = dict(every_frame_preprocessed=sum(w['preprocessing_kernels'] for w in workers)==summary['submitted'],
        every_frame_graph_replayed=sum(w['graph_replays'] for w in workers)==summary['submitted'],
        no_host_to_device_uploads=not uploads,
        production_trial_passed=summary['outcomes']=={'completed':summary['submitted']}
            and summary['worker_drain_clean'] and summary['tracking_order_correct'])
    if summary.get('registration_complete_before_frames'):
        checks['registration_before_first_preprocessing'] = all(w['registration_before_first_preprocessing'] for w in workers)
    con.close()
    return dict(status='passed' if all(checks.values()) else 'failed', checks=checks, workers=workers,
        hardware_overlap_observed=any(w['graph_launches_overlapping_earlier_same_worker_kernel'] for w in workers),
        interpretation='Real production models on GB10. Per-process NVTX ranges exclude capture and warmup. '
            'Overlap means CPU submits a graph while an earlier graph kernel of that worker executes; '
            'it does not mean same-stream kernels execute simultaneously. DtoH result copies remain.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('summary')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=report(args.database,args.summary)
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['status']=='passed' else 1)
