#!/usr/bin/env python3
"""Summarize valid campaign attempts using the canonical MEC-arrival window.

Usage: python3 tools/campaign_analyze.py --campaign-dir runs/campaigns/NAME
Inputs: scenarios.jsonl, state.sqlite3, diagnostics/mec/jobs.jsonl, and
 diagnostics/{pixel,orin,ue}/ALIAS/events.jsonl. No GPU dependencies required.
"""
import argparse
import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

STAGE_SECONDS = {
    'mec_decode': 'decode_s',
    'mec_queue_wait': 'queue_wait_s',
    'mec_preprocess': 'preprocess_s',
    'mec_gpu_inference': 'inference_s',
    'mec_postprocess': 'postprocess_s',
    'mec_total_model_processing': 'processing_s',
    'mec_reorder_wait': 'reorder_wait_s',
    'mec_tracking': 'tracker_s',
    'mec_result_serialization': 'result_prepare_s',
}
STAGE_INTERVALS = {
    'mec_frame_reception_span': ('first_packet_monotonic_ns', 'complete_reception_monotonic_ns'),
    'mec_reception_to_result_ready': ('complete_reception_monotonic_ns', 'result_ready_monotonic_ns'),
    'mec_first_packet_to_result_ready': ('first_packet_monotonic_ns', 'result_ready_monotonic_ns'),
}
STAGES = [*STAGE_SECONDS, 'mec_cuda_event', *STAGE_INTERVALS]


def read_jsonl(path):
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError as error:
                raise ValueError(f'{path}:{number}: invalid JSON: {error}') from error
            if not isinstance(value, dict):
                raise ValueError(f'{path}:{number}: expected an object')
            yield value


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    x = (len(ordered) - 1) * q
    lo = int(x)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (x - lo)


def stats(values):
    values = [v for v in values if v is not None]
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError('Latency samples must be finite and nonnegative')
    return dict(samples=len(values), mean_ms=mean(values) if values else None,
                median_ms=median(values) if values else None,
                p95_ms=percentile(values, .95), p99_ms=percentile(values, .99))


def frame_key(event):
    return tuple(event.get(k) for k in ('scenario_id', 'attempt_id', 'video_id', 'frame_id'))


def group_id(aliases):
    # Membership, not UE count, distinguishes different combinations of equal size.
    return f"{len(aliases)}ue-" + hashlib.sha256(json.dumps(aliases).encode()).hexdigest()[:10]


def summarize(rows, scenario_ids):
    transmitted = sum(r['transmitted'] for r in rows)
    success = sum(r['slo_success'] for r in rows)
    return dict(scenarios=len(scenario_ids), selected_frames=len(rows),
                transmitted_frames=transmitted,
                received_results=sum(r['result_received'] for r in rows),
                missing_results=sum(r['transmitted'] and not r['result_received'] for r in rows),
                slo_success_frames=success,
                slo_success_rate=success / transmitted if transmitted else None,
                outcomes=dict(Counter(r['outcome'] for r in rows)),
                ue_latency_ms=stats(r['ue_latency_ms'] for r in rows),
                mec_latency_breakdown_ms={s: stats(r[s] for r in rows) for s in STAGES})


def with_applications(rows, scenario_ids, assignments):
    result = summarize(rows, scenario_ids)
    apps = sorted({a['application'] for sid in scenario_ids for a in assignments[sid]})
    result['per_application'] = {
        app: summarize([r for r in rows if r['application'] == app],
                       {sid for sid in scenario_ids if any(a['application'] == app for a in assignments[sid])})
        for app in apps}
    return result


def analyze(campaign_dir, slo_ms=150.0):
    if not math.isfinite(slo_ms) or slo_ms <= 0:
        raise ValueError('SLO threshold must be finite and positive')
    campaign_dir = Path(campaign_dir).resolve()
    diag = campaign_dir / 'diagnostics'
    manifest_path = campaign_dir / 'scenarios.jsonl'
    scenarios = list(read_jsonl(manifest_path))
    manifest = {s['scenario_id']: s for s in scenarios}
    if len(manifest) != len(scenarios):
        raise ValueError('Duplicate scenario IDs in manifest')
    ids = {s['campaign_id'] for s in scenarios}
    if len(ids) != 1:
        raise ValueError('Manifest must contain one campaign ID')
    campaign_id = ids.pop()
    lock_path = campaign_dir / 'manifest.lock.json'
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != lock['scenarios_sha256']:
            raise ValueError('Manifest hash does not match its lock')
    with sqlite3.connect((campaign_dir / 'state.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        states = [dict(row) for row in db.execute('SELECT * FROM scenarios')]
    valid = {(r['scenario_id'], r['attempt_id']): r for r in states if r['status'] == 'valid'}
    if not valid:
        raise ValueError('No valid scenarios in the checkpoint')
    evidence = {}; groups = {}; scenario_groups = {}; expected_runs = {}; video_map = {}
    for (sid, aid), state in valid.items():
        if sid not in manifest or not aid:
            raise ValueError(f'Invalid checkpoint identity: {sid}/{aid}')
        ev = json.loads(state['evidence_json'] or '{}')
        start = ev.get('release', {}).get('start_monotonic_ns')
        if type(start) is not int or start < 0:
            raise ValueError(f'{sid}/{aid}: missing MEC release timestamp')
        evidence[(sid, aid)] = ev
        aliases = sorted(u['ue_alias'] for u in manifest[sid]['ues'])
        if not aliases or len(aliases) != len(set(aliases)):
            raise ValueError(f'{sid}: empty or duplicate UE membership')
        gid = group_id(aliases)
        groups.setdefault(gid, {'ue_aliases': aliases, 'scenario_ids': []})['scenario_ids'].append(sid)
        scenario_groups[sid] = gid
        expected_runs[(sid, aid)] = ev.get('confirmation', {}).get('association_snapshot', {}).get('run_id')
        for vid, video in ev.get('videos', {}).items():
            video_map[(sid, aid, vid)] = video
    inputs = [str(manifest_path), str(campaign_dir / 'state.sqlite3')]
    duplicates = Counter(); runs = set()

    def selected(path):
        inputs.append(str(path))
        for event in read_jsonl(path):
            context = (event.get('scenario_id'), event.get('attempt_id'))
            if context not in valid or event.get('campaign_id') != campaign_id:
                continue
            expected = expected_runs[context]
            if expected is not None and event.get('run_id') != expected:
                raise ValueError(f'{path}: run ID conflicts with checkpoint for {context}')
            if not event.get('run_id'):
                raise ValueError(f'{path}: selected event lacks run ID')
            runs.add(event['run_id'])
            yield event

    jobs = {}
    for event in selected(diag / 'mec/jobs.jsonl'):
        if event.get('type') != 'frame_result':
            continue
        key = frame_key(event)
        if key in jobs:
            duplicates['jobs'] += 1
            if jobs[key] != event:
                raise ValueError(f'Conflicting MEC job records: {key}')
        else:
            jobs[key] = event
    txs = {}; receipts = {}; event_aliases = {}
    for alias in sorted({u for g in groups.values() for u in g['ue_aliases']}):
        # Prefer the current layout. Legacy ue/ is only a fallback.
        preferred = [diag / family / alias / 'events.jsonl' for family in ('pixel', 'orin')]
        matches = [p for p in preferred if p.exists()]
        if not matches:
            matches = [diag / 'ue' / alias / 'events.jsonl']
        if len(matches) != 1 or not matches[0].is_file():
            raise ValueError(f'Missing or ambiguous UE event file for {alias}: {matches}')
        for event in selected(matches[0]):
            kind = event.get('type')
            if kind not in ('transmitted', 'result_receipt'):
                continue
            key = frame_key(event)
            if alias not in groups[scenario_groups[key[0]]]['ue_aliases']:
                raise ValueError(f'Inactive UE has frame events: {key}/{alias}')
            if key in event_aliases and event_aliases[key] != alias:
                raise ValueError(f'Frame belongs to multiple UEs: {key}')
            event_aliases[key] = alias
            dest, field = (txs, 'tx_mono_ns') if kind == 'transmitted' else (receipts, 'receipt_mono_ns')
            timestamp = event.get(field)
            if type(timestamp) is not int or timestamp < 0:
                raise ValueError(f'{key}: missing or invalid {field}')
            if key in dest:
                duplicates[kind] += 1
                if timestamp < dest[key][field]:
                    dest[key] = event
            else:
                dest[key] = event
    rows = []; window = Counter(); per_attempt = []; warnings = []
    keys_by_attempt = defaultdict(list)
    for key in jobs.keys() | txs.keys() | receipts.keys():
        keys_by_attempt[key[:2]].append(key)
    for context, state in sorted(valid.items()):
        sid, aid = context; ev = evidence[context]
        start = ev['release']['start_monotonic_ns']; counts = Counter()
        if not keys_by_attempt[context]:
            raise ValueError(f'No frame evidence for valid attempt {context}')
        for key in sorted(keys_by_attempt[context]):
            job = jobs.get(key, {}); stage = job.get('stage_times') or {}
            arrival = stage.get('first_packet_monotonic_ns')
            if type(arrival) is not int or arrival < 0:
                counts['unknown_window'] += 1
                continue
            if not start + 10**9 <= arrival < start + 6 * 10**9:
                counts['outside_window'] += 1
                continue
            counts['selected'] += 1
            tx, receipt = txs.get(key), receipts.get(key)
            video = video_map.get(key[:3], {})
            alias = video.get('ue_alias', event_aliases.get(key))
            if alias is None or alias not in groups[scenario_groups[sid]]['ue_aliases']:
                raise ValueError(f'Cannot identify frame UE: {key}')
            if key in event_aliases and event_aliases[key] != alias:
                raise ValueError(f'UE identity mismatch: {key}')
            if tx is None:
                raise ValueError(f'Selected frame missing UE transmission evidence: {key}')
            assignment = next(u for u in manifest[sid]['ues'] if u['ue_alias'] == alias)
            if job.get('application') != assignment['application']:
                raise ValueError(f'Application mismatch: {key}')
            latency = (receipt['receipt_mono_ns'] - tx['tx_mono_ns']) / 1e6 if receipt else None
            if latency is not None and latency < 0:
                raise ValueError(f'Negative UE latency: {key}')
            if receipt and receipt.get('outcome') != job.get('outcome'):
                raise ValueError(f'MEC/UE outcome mismatch: {key}')
            success = bool(receipt and receipt.get('outcome') == 'completed' and latency <= slo_ms)
            row = dict(ue_group=scenario_groups[sid], campaign_id=campaign_id,
                       scenario_id=sid, attempt_id=aid, ue_alias=alias,
                       video_id=key[2], frame_id=key[3], application=assignment['application'],
                       candidate_id=job.get('candidate_id'), arrival_offset_s=(arrival-start)/1e9,
                       transmitted=True, result_received=bool(receipt), ue_latency_ms=latency,
                       outcome=job.get('outcome'), failure_reason=job.get('reason'), slo_success=success)
            for name, field in STAGE_SECONDS.items():
                row[name] = stage[field]*1000 if stage.get(field) is not None else None
            row['mec_cuda_event'] = stage.get('cuda_event_ms')
            for name, (begin, end) in STAGE_INTERVALS.items():
                row[name] = (stage[end]-stage[begin])/1e6 if stage.get(begin) is not None and stage.get(end) is not None else None
            rows.append(row)
        window.update(counts)
        per_attempt.append(dict(scenario_id=sid, attempt_id=aid, ue_group=scenario_groups[sid], **counts))
        if not counts['selected']:
            warnings.append(f'{sid}/{aid}: no frames selected in the collection window')
    if len(runs) != 1:
        raise ValueError(f'Selected evidence spans multiple MEC runs: {sorted(runs)}')
    missing = sum(not r['result_received'] for r in rows)
    if missing:
        warnings.append(f'{missing} selected transmissions lack result receipts; counted as SLO failures, excluded from latency statistics')
    if window['unknown_window']:
        warnings.append(f"{window['unknown_window']} frame keys lack usable MEC first-packet timestamps; excluded, not counted as successes")
    result = dict(schema_version=1, campaign_id=campaign_id, run_id=next(iter(runs)),
                  campaign_directory=str(campaign_dir), slo_threshold_ms=slo_ms,
                  selection='Accepted valid attempts only; 1 <= (MEC first packet arrival - MEC scenario release) / 1e9 < 6',
                  latency_definition='First UE result receipt minus UE transmission timestamp; includes completed and failed result receipts',
                  percentile_definition='Linear interpolation at (n-1)*q; pooled samples are frame-weighted',
                  slo_definition='Completed result with UE latency <= threshold / selected transmitted frames; missing receipts fail',
                  mec_timing_notes='Same-host durations. Missing stages excluded, not zero. CUDA event overlaps inference; processing includes pre/inference/post, excludes reorder/tracking. Serialization excludes disk replay, socket queue, network.',
                  window_counts=dict(window), checkpoint_status_counts=dict(Counter(r['status'] for r in states)),
                  duplicates_ignored=dict(duplicates), warnings=warnings, inputs=inputs,
                  attempts=per_attempt, groups={})
    for gid, info in sorted(groups.items()):
        group_rows = [r for r in rows if r['ue_group'] == gid]
        assignments = {sid: manifest[sid]['ues'] for sid in info['scenario_ids']}
        result['groups'][gid] = dict(**info, ue_count=len(info['ue_aliases']),
            overall=with_applications(group_rows, info['scenario_ids'], assignments),
            per_ue={alias: with_applications([r for r in group_rows if r['ue_alias'] == alias], info['scenario_ids'],
                                            {sid: [a for a in values if a['ue_alias'] == alias] for sid, values in assignments.items()})
                    for alias in info['ue_aliases']})
    output = diag / 'results'
    output.mkdir(parents=True, exist_ok=True)
    for gid, group in result['groups'].items():
        (output / f'{gid}-summary.json').write_text(json.dumps(dict(campaign_id=campaign_id, run_id=result['run_id'], **group), indent=2, allow_nan=False)+'\n')
    (output / 'summary.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    with (output / 'frames.csv').open('w', newline='') as stream:
        fields = list(rows[0]) if rows else ['ue_group','ue_alias','application','ue_latency_ms',*STAGES]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(sorted(rows, key=lambda r:(r['ue_group'],r['scenario_id'],r['ue_alias'],r['frame_id'])))
    write_markdown(output / 'latency_summary.md', result)
    return result


def write_markdown(path, result):
    lines = [f"# Latency summary: {result['campaign_id']}", '',
             f"MEC run: `{result['run_id']}`. SLO: {result['slo_threshold_ms']:g} ms.", '',
             result['selection']+'. Results received after the window are retained.', '',
             result['latency_definition']+'. Missing results are excluded from latency statistics but fail the SLO.', '',
             'All latency values are milliseconds; percentiles use linear interpolation.', '']
    def fmt(value):
        return '—' if value is None else f'{value:.3f}'
    for gid, group in result['groups'].items():
        lines += [f"## {group['ue_count']} UEs: {', '.join(group['ue_aliases'])}", '',
                  f"{group['overall']['scenarios']} valid scenarios. JSON: [{gid}-summary.json]({gid}-summary.json).", '',
                  '| UE / application | Transmitted | Receipts | Mean | Median | P95 | P99 | SLO success |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|']
        scopes = [('All UEs', group['overall']), *group['per_ue'].items()]
        for label, summary in scopes:
            for name, data in [(label,summary), *((label+' / '+a,v) for a,v in summary['per_application'].items())]:
                st=data['ue_latency_ms'];rate=data['slo_success_rate']
                lines.append('| '+ ' | '.join([name,str(data['transmitted_frames']),str(data['received_results']),
                    *(fmt(st[k]) for k in ('mean_ms','median_ms','p95_ms','p99_ms')),
                    '—' if rate is None else f'{rate:.2%}'])+' |')
        lines += ['', '### MEC timing (all UEs)', '', '| Stage | Samples | Mean | Median | P95 | P99 |', '|---|---:|---:|---:|---:|---:|']
        for stage, st in group['overall']['mec_latency_breakdown_ms'].items():
            lines.append('| '+' | '.join([stage,str(st['samples']),*(fmt(st[k]) for k in ('mean_ms','median_ms','p95_ms','p99_ms'))])+' |')
        lines += ['', 'Per-UE and per-application MEC breakdowns are included in the JSON summary.', '']
    lines += ['## Coverage and definitions', '', f"Window counts: `{result['window_counts']}`.", '', result['mec_timing_notes'], '',
              'CDFs include only observed latency samples; missing receipts/stages are not zero latency. EdgeRIC logs are not required for these latency metrics.', '']
    lines += ['- '+w for w in result['warnings']]
    path.write_text('\n'.join(lines)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign-dir', required=True, type=Path)
    parser.add_argument('--slo-ms', type=float, default=150.0)
    args = parser.parse_args()
    result = analyze(args.campaign_dir, args.slo_ms)
    print(f"Analyzed {len(result['attempts'])} valid scenarios, {result['window_counts'].get('selected', 0)} selected frames")
    print(args.campaign_dir / 'diagnostics/results/latency_summary.md')
    for warning in result['warnings']:
        print('WARNING:', warning)


if __name__ == '__main__':
    main()
