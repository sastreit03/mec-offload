import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from campaign_analyze import analyze, stats
from campaign_plot import ecdf


def jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(v)+'\n' for v in values))


@pytest.fixture
def campaign(tmp_path):
    scenarios=[];jobs=[];events={};states=[];sends=[]
    for index, aliases in enumerate([['pixel1'], ['pixel1','pixel2'], ['pixel1','linux1']]):
        sid=f's{index}';aid=f'a{index}'
        assignments=[dict(ue_alias=a,application='detection') for a in aliases]
        scenarios.append(dict(campaign_id='c',scenario_id=sid,ues=assignments))
        videos={}
        for alias in aliases:
            vid=f'{sid}-{alias}';videos[vid]=dict(ue_alias=alias,application='detection')
            for fid, offset in enumerate([999999999,1000000000,5999999999,6000000000,None]):
                context=dict(campaign_id='c',scenario_id=sid,attempt_id=aid,run_id='run',video_id=vid,frame_id=fid, result_id=f'{sid}-{alias}-{fid}')
                sends.append(dict(context,type='send_attempt',message_type='frame_result',monotonic_ns=16*10**9))
                stage={} if offset is None else dict(first_packet_monotonic_ns=10**10+offset,decode_s=.002)
                outcome='failed' if fid==2 else 'completed'
                jobs.append(dict(context,type='frame_result',application='detection',outcome=outcome,stage_times=stage))
                events.setdefault(alias,[]).append(dict(context,type='transmitted',tx_mono_ns=10**9))
                if fid!=2:
                    events[alias].append(dict(context,type='result_receipt',receipt_mono_ns=1100000000,outcome=outcome))
                    if fid==1:  # A later replay must not replace the first receipt.
                        events[alias].append(dict(context,type='result_receipt',receipt_mono_ns=2000000000,outcome=outcome))
            # Invalid retry with same scenario/video/frame identity must be excluded.
            bad=dict(context,attempt_id='bad',frame_id=1)
            jobs.append(dict(bad,type='frame_result',application='detection',outcome='completed',stage_times={'first_packet_monotonic_ns':11000000000}))
            events[alias].append(dict(bad,type='transmitted',tx_mono_ns=1))
        ev=dict(release=dict(start_monotonic_ns=10**10),videos=videos,
                confirmation=dict(association_snapshot=dict(run_id='run')))
        states.append((sid,'valid',aid,json.dumps(ev)))
    scenarios.append(dict(campaign_id='c',scenario_id='pending',ues=[dict(ue_alias='absent',application='tracking')]))
    states.append(('pending','pending',None,'{}'))
    jsonl(tmp_path/'scenarios.jsonl',scenarios)
    (tmp_path/'manifest.lock.json').write_text(json.dumps(dict(scenarios_sha256=hashlib.sha256((tmp_path/'scenarios.jsonl').read_bytes()).hexdigest())))
    with sqlite3.connect(tmp_path/'state.sqlite3') as db:
        db.execute('CREATE TABLE scenarios (scenario_id TEXT, status TEXT, attempt_id TEXT, evidence_json TEXT)')
        db.executemany('INSERT INTO scenarios VALUES (?,?,?,?)',states)
    jsonl(tmp_path/'diagnostics/mec/jobs.jsonl',jobs)
    jsonl(tmp_path/'diagnostics/mec/events.jsonl',sends)
    for alias,records in events.items():
        family='orin' if alias=='linux1' else 'pixel'
        jsonl(tmp_path/f'diagnostics/{family}/{alias}/events.jsonl',records)
    return tmp_path


def test_valid_selection_window_membership_and_missing_receipts(campaign):
    result=analyze(campaign)
    assert len(result['groups'])==3  # Two distinct pairs are not collapsed.
    assert result['window_counts']==dict(selected=10,arrival_window=10,outside_window=10,unknown_window=5)
    assert result['duplicates_ignored']['result_receipt']==5
    assert len(result['attempts'])==3
    for group in result['groups'].values():
        st=group['overall'];n=group['ue_count']
        assert st['transmitted_frames']==2*n
        assert st['missing_results']==n
        assert st['slo_success_rate']==.5
        assert st['ue_latency_ms']==dict(samples=n,mean_ms=100,median_ms=100,p95_ms=100,p99_ms=100)
        assert st['mec_latency_breakdown_ms']['mec_decode']['samples']==2*n
        assert st['mec_latency_breakdown_ms']['mec_tracking']['samples']==0
    assert (campaign/'diagnostics/results/latency_summary.md').is_file()
    # Each Markdown table must have matching header, separator, and row widths.
    width = None
    for line in (campaign/'diagnostics/results/latency_summary.md').read_text().splitlines():
        if line.startswith('|'):
            if width is None:
                width = line.count('|')
            assert line.count('|') == width
        else:
            width = None


def test_missing_transmission_is_not_silently_excluded(campaign):
    p=campaign/'diagnostics/pixel/pixel1/events.jsonl'
    events=[json.loads(l) for l in p.read_text().splitlines()]
    jsonl(p,[e for e in events if not (e['type']=='transmitted' and e['scenario_id']=='s0' and e['frame_id']==1)])
    with pytest.raises(ValueError,match='missing UE transmission'):
        analyze(campaign)


def test_manifest_integrity(campaign):
    with (campaign/'scenarios.jsonl').open('a') as stream:stream.write('\n')
    with pytest.raises(ValueError,match='Manifest hash'):
        analyze(campaign)


def test_statistics_and_ecdf():
    assert stats([None])==dict(samples=0,mean_ms=None,median_ms=None,p95_ms=None,p99_ms=None)
    s=stats([0,100]);assert s['median_ms']==50 and s['p95_ms']==95 and s['p99_ms']==99
    assert ecdf([3,1,1])==([1,1,1,3],[0,1/3,2/3,1])
    assert ecdf([])==([],[])


def test_send_cutoff_duplicates_context_and_late_receipt(campaign):
    p=campaign/'diagnostics/mec/events.jsonl'
    sends=[json.loads(l) for l in p.read_text().splitlines()]
    # s0 frame 1 is late; a different attempt's early send must not rescue it.
    for e in sends:
        if e['scenario_id']=='s0' and e['frame_id']==1:
            e['monotonic_ns']=16*10**9+1
            sends.append(dict(e,attempt_id='invalid',monotonic_ns=12*10**9))
            break
    # s1 pixel1 frame 1 has no send evidence; s1 pixel2 frame 1 has an
    # early send and a late replay. Preserve the early send regardless of order.
    sends=[e for e in sends if not (e['scenario_id']=='s1' and e['video_id']=='s1-pixel1' and e['frame_id']==1)]
    e=next(e for e in sends if e['scenario_id']=='s1' and e['video_id']=='s1-pixel2' and e['frame_id']==1)
    sends.insert(0,dict(e,monotonic_ns=20*10**9))
    jsonl(p,sends)
    # UE clock is independent: a receipt many seconds after its transmission is
    # still included if the corresponding MEC send met the cutoff.
    p=campaign/'diagnostics/pixel/pixel2/events.jsonl'
    events=[json.loads(l) for l in p.read_text().splitlines()]
    for e in events:
        if e['type']=='result_receipt': e['receipt_mono_ns']=30*10**9
    jsonl(p,events)
    result=analyze(campaign)
    assert result['window_counts']['selected']==8
    assert result['window_counts']['sent_after_cutoff']==1
    assert result['window_counts']['missing_send_evidence']==1
    group=next(g for g in result['groups'].values() if g['ue_aliases']==['pixel1','pixel2'])
    assert group['overall']['transmitted_frames']==3
    assert group['overall']['mec_latency_breakdown_ms']['mec_decode']['samples']==3
    assert group['per_ue']['pixel2']['ue_latency_ms']['mean_ms']==29000
    assert group['overall']['slo_success_rate']==0


def test_missing_mec_events_file_fails(campaign):
    (campaign/'diagnostics/mec/events.jsonl').unlink()
    with pytest.raises(FileNotFoundError): analyze(campaign)
