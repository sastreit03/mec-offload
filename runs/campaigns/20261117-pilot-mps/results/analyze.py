"""Reproduce pilot summaries using the canonical MEC-arrival collection window."""
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / 'multi-ue/runs/diagnostics/20261117-pilot-mps-review'
OUT = Path(__file__).resolve().parent
CAMP = SOURCE / 'ran/20261117-pilot-mps'
db = sqlite3.connect(f'file:{CAMP}/state.sqlite3?mode=ro', uri=True)
db.row_factory = sqlite3.Row
states = {r['attempt_id']: dict(r) for r in db.execute("SELECT * FROM scenarios WHERE status='valid'")}
manifest = {s['scenario_id']: s for s in map(json.loads, (CAMP/'scenarios.jsonl').open())}
evidence = {a: json.loads(r['evidence_json']) for a,r in states.items()}
latency_deltas = []; slo_disagreements = []; issues = []; duplicates = Counter(); inputs = []
def records(path):
    inputs.append(str(path.relative_to(ROOT)))
    with path.open() as f:
        for number,line in enumerate(f,1):
            try: e=json.loads(line)
            except ValueError:
                issues.append(f'{path}:{number}: invalid JSON');continue
            if e.get('attempt_id') in states and e.get('campaign_id')=='20261117-pilot-mps':
                yield e

def key(e): return (e['attempt_id'], e.get('video_id'), e.get('frame_id'))
jobs={}
for e in records(SOURCE/'mec/20261117-pilot-mps-collection/mec/jobs.jsonl'):
    k=key(e)
    if k in jobs: duplicates['jobs']+=1
    else: jobs[k]=e
transmissions={}; receipts={}; aliases={}
for alias,path in [(f'pixel{i}',SOURCE/f'ue/pixel{i}/events.jsonl') for i in (1,2,3)]+[('linux1',SOURCE/'orin/20261117-pilot-mps/ue/linux1/events.jsonl')]:
    for e in records(path):
        if e['type'] not in ('transmitted','result_receipt'):continue
        k=key(e);dest=transmissions if e['type']=='transmitted' else receipts
        if k in dest:
            duplicates[e['type']]+=1
            # First receipt gives the observed end-to-end latency, not later replay.
            field='tx_mono_ns' if e['type']=='transmitted' else 'receipt_mono_ns'
            if e.get(field,float('inf'))<dest[k].get(field,float('inf')):dest[k]=e
        else:dest[k]=e
        aliases[k]=alias

def quantile(values,q):
    v=sorted(values)
    if not v:return None
    x=(len(v)-1)*q;i=int(x);j=min(i+1,len(v)-1)
    return v[i]+(v[j]-v[i])*(x-i)
def stats(values):
    v=[x for x in values if x is not None]
    return dict(samples=len(v),mean_ms=mean(v) if v else None,p99_ms=quantile(v,.99))

stages={'mec_decode':'decode_s','mec_queue_wait':'queue_wait_s','mec_preprocess':'preprocess_s',
 'mec_gpu_inference':'inference_s','mec_postprocess':'postprocess_s','mec_total_model_processing':'processing_s',
 'mec_reorder_wait':'reorder_wait_s','mec_tracking':'tracker_s','mec_result_serialization':'result_prepare_s'}
rows=[]; window=Counter(); attempts=[]
for a,state in states.items():
    ev=evidence[a];sc=manifest[state['scenario_id']];start=ev['release']['start_monotonic_ns']
    snap=ev.get('confirmation',{}).get('association_snapshot',{})
    if snap.get('association_losses') or snap.get('telemetry_errors') or snap.get('evidence_errors'):
        issues.append(f'{a}: finalization association errors')
    count=0
    for k in set(jobs)|set(transmissions):
        if k[0]!=a:continue
        job=jobs.get(k,{});stage=job.get('stage_times',{});arrival=stage.get('first_packet_monotonic_ns')
        if not isinstance(arrival,int) or arrival<0:window['unknown_window']+=1;continue
        if not start+10**9<=arrival<start+6*10**9:window['outside_window']+=1;continue
        window['selected']+=1;count+=1
        tx=transmissions.get(k);receipt=receipts.get(k)
        video=ev['videos'].get(k[1],{});alias=video.get('ue_alias',aliases.get(k))
        if not tx:issues.append(f'{k}: selected job missing transmitted event')
        latency=(receipt['receipt_mono_ns']-tx['tx_mono_ns'])/1e6 if receipt and tx else None
        if latency is not None and latency<0:issues.append(f'{k}: negative UE latency')
        if latency is not None and receipt.get('latency_ns') is not None:
            latency_deltas.append(latency-receipt['latency_ns']/1e6)
        outcome=receipt.get('outcome') if receipt else job.get('outcome')
        success=bool(tx and receipt and outcome=='completed' and latency is not None and 0<=latency<=150)
        if receipt and success != receipt.get('slo_success'):
            slo_disagreements.append(dict(attempt_id=a,video_id=k[1],frame_id=k[2],computed=success,logged=receipt.get('slo_success')))
        r=dict(group='single_ue' if len(sc['ues'])==1 else 'four_ue',scenario_id=state['scenario_id'],attempt_id=a,
               ue_alias=alias,video_id=k[1],frame_id=k[2],application=job.get('application',video.get('application')),
               candidate_id=job.get('candidate_id'),arrival_offset_s=(arrival-start)/1e9,
               transmitted=bool(tx),result_received=bool(receipt),latency_ms=latency,outcome=outcome,
               failure_reason=job.get('reason'),slo_success=success)
        for name,field in stages.items():r[name]=stage[field]*1000 if stage.get(field) is not None else None
        r['mec_cuda_event']=stage.get('cuda_event_ms')
        for name,end,begin in [('mec_frame_reception_span','complete_reception_monotonic_ns','first_packet_monotonic_ns'),
                              ('mec_reception_to_result_ready','result_ready_monotonic_ns','complete_reception_monotonic_ns'),
                              ('mec_first_packet_to_result_ready','result_ready_monotonic_ns','first_packet_monotonic_ns')]:
            r[name]=(stage[end]-stage[begin])/1e6 if stage.get(end) is not None and stage.get(begin) is not None else None
        rows.append(r)
    attempts.append(dict(scenario_id=state['scenario_id'],attempt_id=a,ue_count=len(sc['ues']),selected_frames=count,
                         association_finalized=snap.get('status')=='association_finalized',source_generation=snap.get('source_generation')))

stage_names=[k for k in rows[0] if k.startswith('mec_')]
def summary(rs):
    n=sum(r['transmitted'] for r in rs);lat=stats([r['latency_ms'] for r in rs]);success=sum(r['slo_success'] for r in rs)
    return dict(scenarios=len({r['scenario_id'] for r in rs}),selected_frames=len(rs),transmitted_frames=n,
        received_results=sum(r['result_received'] for r in rs),missing_results=sum(r['transmitted'] and not r['result_received'] for r in rs),
        latency_samples=lat['samples'],mean_latency_ms=lat['mean_ms'],p99_latency_ms=lat['p99_ms'],
        slo_success_frames=success,slo_success_rate=success/n if n else None,outcomes=dict(Counter(r['outcome'] for r in rs)),
        mec_latency_breakdown_ms={k:stats([r[k] for r in rs]) for k in stage_names})

def with_apps(rs):
    return dict(**summary(rs),per_application={a:summary([r for r in rs if r['application']==a]) for a in sorted({r['application'] for r in rs})})
result=dict(campaign_id='20261117-pilot-mps',run_id='20261117-pilot-mps-collection',
    methodology=dict(window='1 <= (MEC first packet arrival - MEC scenario release) / 1e9 < 6',
        latency='UE result_receipt.receipt_mono_ns - transmitted.tx_mono_ns; first receipt; includes failed outcomes with receipts',
        slo='completed result with UE latency <= 150 ms / selected transmitted frames; missing receipts fail',
        percentile='linear interpolation at (n-1)*0.99',pooling='frame-weighted, not average of UE or scenario percentiles',
        timing='MEC stages use same-host durations; stages have different sample populations and may overlap; do not sum percentiles',
        selection='only runner-selected valid attempts; unknown arrival membership excluded and counted',
        model_processing='preprocess through postprocess incl. worker overhead; excludes tracking/reorder',
        serialization='JSON preparation only, excludes replay disk write, websocket queue and network'),
    logged_latency_difference_ms=stats(latency_deltas),slo_flag_disagreements=slo_disagreements,
    window_counts=dict(window),duplicates_ignored=dict(duplicates),issues=issues,inputs=inputs,attempts=attempts)
for group in ('single_ue','four_ue'):
    rs=[r for r in rows if r['group']==group]
    result[group]=dict(overall=with_apps(rs),per_ue={u:with_apps([r for r in rs if r['ue_alias']==u]) for u in sorted({r['ue_alias'] for r in rs})})
    (OUT/f'{group}-summary.json').write_text(json.dumps(result[group],indent=2)+'\n')
(OUT/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
rows.sort(key=lambda r:(r['group'],r['scenario_id'],r['ue_alias'],r['frame_id']))
with (OUT/'frames.csv').open('w') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
lines=['# Pilot campaign results','', 'Only accepted valid attempts; frames selected by MEC first packet arrival in [1 s, 6 s) after scenario release. Results arriving later remain included. SLO: completed result received within 150 ms, divided by selected transmitted frames. P99 uses linear interpolation.','']
for group in ('single_ue','four_ue'):
    g=result[group];lines+=['## '+('Single UE' if group=='single_ue' else 'Four UEs'),'','| UE / application | Transmitted | Receipts | Mean ms | P99 ms | SLO success |','|---|---:|---:|---:|---:|---:|']
    def line(label,s):return f"| {label} | {s['transmitted_frames']} | {s['received_results']} | {s['mean_latency_ms']:.3f} | {s['p99_latency_ms']:.3f} | {100*s['slo_success_rate']:.2f}% |"
    lines.append(line('All UEs',g['overall']))
    for u,s in g['per_ue'].items():lines.append(line(u,s))
    for app,s in g['overall']['per_application'].items():lines.append(line('All / '+app,s))
    for u,s in g['per_ue'].items():
        for app,v in s['per_application'].items():lines.append(line(u+' / '+app,v))
    lines+=['','### MEC breakdown (all UEs)','','| Stage | Samples | Mean ms | P99 ms |','|---|---:|---:|---:|']
    for k,s in g['overall']['mec_latency_breakdown_ms'].items():
        lines.append(f"| {k} | {s['samples']} | {s['mean_ms']:.3f} | {s['p99_ms']:.3f} |" if s['samples'] else f'| {k} | 0 | — | — |')
    lines+=['']
lines+=['## Evidence and limitations','',f"Window accounting: `{dict(window)}`. Issues: {len(issues)}. See summary.json for source paths, accepted attempts, duplicates, and methodology.",'','MEC breakdowns per UE and application are in the JSON summaries. Linux logs compute latency and receipt timestamps with separate clock reads; this report consistently recomputes latency from the logged receipt and transmission timestamps. Missing stage samples are not zero latency. CUDA event and inference timings overlap; do not add them. These aggregate historical pilot deployments, not a single uniform software version. EdgeRIC finalization confirmations are checked from runner evidence; raw EdgeRIC logs are not needed for these latency metrics and have not been exhaustively audited.','', 'Reproduce from repository root:','', '```bash','python3 runs/campaigns/20261117-pilot-mps/results/analyze.py','```']
(OUT/'README.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({g:result[g]['overall'] for g in ('single_ue','four_ue')},indent=2))
print('Issues:',issues)
