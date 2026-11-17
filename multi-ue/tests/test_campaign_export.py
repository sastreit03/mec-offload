import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import export_campaign_dataset as export


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_frame_joins_every_overlapping_ran_and_mec_window(tmp_path):
    campaign = "campaign"
    scenario_id = "scenario"
    attempt = "attempt"
    video = "video"
    ue_id = "ue"
    context = dict(
        campaign_id=campaign, scenario_id=scenario_id, attempt_id=attempt
    )
    scenario = {
        "campaign_id": campaign,
        "scenario_id": scenario_id,
        "execution_index": 1,
        "duration_s": 5,
        "fps": 30,
        "gop_frames": 30,
        "factors": {
            "ue_set": "one_ue",
            "application_rotation": 0,
            "tier": 0,
        },
        "radio": {
            "profile_id": "band78-51prb-threequarter",
            "carrier_prbs": 51,
            "nominal_bandwidth_mhz": 20,
            "center_frequency_hz": 3_309_480_000,
            "subcarrier_spacing_hz": 30_000,
            "threequarter_sampling": True,
            "fft_size": 768,
            "sample_rate_hz": 23_040_000,
        },
        "channel": {
            "condition_id": "awgn-sinr20",
            "model": "AWGN",
            "sinr_db": 20,
            "delay_spread_ns": None,
            "speed_mps": None,
        },
        "ues": [{
            "ue_alias": "pixel1",
            "application": "detection",
            "candidate_id": "detection-yolo26n-4000k",
            "bitrate_bps": 4_000_000,
            "frame_count": 1,
        }],
    }
    states = {
        scenario_id: ("valid", 1, attempt, 1, 2, None, "{}"),
    }
    events = tmp_path / "mec-events.jsonl"
    write_jsonl(events, [
        {**context, "type": "descriptor", "video_id": video, "frame_id": 0,
         "unix_ns": 225, "tx_unix_ns": 105, "monotonic_ns": 10,
         "encoded_bytes": 1000, "frame_type": 1},
        {**context, "type": "mec_resources", "snapshot": {
            "window_start_unix_ns": 0, "window_end_unix_ns": 100,
            "snapshot_monotonic_ns": 1100, "streams": [], "workers": {},
            "gpu": {}, "cpu": {"valid": True, "utilization_percent": 28.4,
                                    "per_cpu_utilization_percent": [41.2, 15.6]}}},
        {**context, "type": "mec_resources", "snapshot": {
            "window_start_unix_ns": 100, "window_end_unix_ns": 200,
            "snapshot_monotonic_ns": 1200, "streams": [], "workers": {},
            "gpu": {}}},
    ])
    ran = tmp_path / "ran-events.jsonl"
    reports = []
    for sequence, start, end in ((1, 100, 200), (2, 200, 300)):
        reports.append({"type": "radio_report", "report": {
            **context, "sequence": sequence, "window_start_unix_ns": start,
            "window_end_unix_ns": end, "ues": {ue_id: {
                "rnti": 1, "association_valid": True, "scheduled_prbs": 5,
                "scheduled_tbs_bytes": 100, "radio_state": {},
            }}}})
    write_jsonl(ran, reports)
    jobs = tmp_path / "jobs.jsonl"
    write_jsonl(jobs, [{
        **context, "video_id": video, "frame_id": 0, "ue_id": ue_id,
        "application": "detection", "candidate_id": "detection-yolo26n-4000k",
        "model": "yolo26n", "bitrate_bps": 4_000_000,
        "outcome": "completed", "stage_times": {
            "first_packet_monotonic_ns": 1050,
            "result_ready_monotonic_ns": 1150,
        },
    }])

    db = export.create_database(tmp_path / "dataset.sqlite3")
    db.execute(
        "INSERT INTO scenarios VALUES (" + ",".join("?" * 31) + ")",
        (
            campaign, scenario_id, 1, "valid", 1, attempt, 1, 2, None,
            "one_ue", 0, 0, "null", "band78-51prb-threequarter", 51, 20,
            3_309_480_000, 30_000, True, 768, 23_040_000,
            "awgn-sinr20", "AWGN", 20, None, None,
            5, 30, 30, json.dumps(scenario), "{}",
        ),
    )
    export.ingest_mec_events(db, [events])
    export.ingest_ran(db, [ran])
    export.ingest_jobs(
        db, [jobs], {scenario_id: scenario}, states,
        {(scenario_id, attempt, video): ("pixel1", ue_id)},
        {scenario_id: {"pixel1": scenario["ues"][0]}},
    )

    assert db.execute("SELECT count(*) FROM frames").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM frame_ran_windows").fetchone()[0] == 2
    assert db.execute("SELECT sum(overlap_ns) FROM frame_ran_windows").fetchone()[0] == 120
    assert db.execute("SELECT count(*) FROM frame_mec_windows").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM frame_ran_features").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM frame_mec_features").fetchone()[0] == 2
    resources = db.execute("SELECT cpu_json FROM mec_resources ORDER BY id").fetchall()
    assert json.loads(resources[0][0])["per_cpu_utilization_percent"] == [41.2, 15.6]
    assert json.loads(resources[1][0]) is None  # Older logs remain exportable.
    assert db.execute("SELECT cpu_json FROM frame_mec_features ORDER BY mec_window_start_unix_ns").fetchall() == resources
    export.export_table(db, "frame_mec_features", tmp_path / "features.csv")
    import csv
    with (tmp_path / "features.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert json.loads(rows[0]["cpu_json"])["utilization_percent"] == 28.4
    db.close()


def test_timed_attempt_export_uses_evidence_totals_and_preserves_counts(tmp_path, monkeypatch):
    import sqlite3
    scenario = dict(campaign_id="c", scenario_id="s", execution_index=1,
                    factors=dict(ue_set="one", application_rotation=0),
                    channel=dict(condition_id="channel", model="AWGN"),
                    duration_s=5, fps=10, gop_frames=10,
                    ues=[dict(ue_alias="pixel1", frame_count=150)])
    write_jsonl(tmp_path / "scenarios.jsonl", [scenario])
    evidence = dict(release=dict(start_monotonic_ns=10_000_000_000),
                    videos={"v": dict(ue_alias="pixel1", counters={"completed": 2},
                                      frames_fully_sent=2, frames_fully_received=2,
                                      frame_counts_validated=True)})
    state = sqlite3.connect(tmp_path / "state.sqlite3")
    state.execute("CREATE TABLE scenarios (scenario_id, status, attempts, attempt_id, "
                  "started_unix_ns, finished_unix_ns, error, evidence_json)")
    state.execute("INSERT INTO scenarios VALUES (?,?,?,?,?,?,?,?)",
                  ("s", "valid", 2, "good", 1, 2, None, json.dumps(evidence)))
    state.commit()
    state.close()
    db = export.create_database(tmp_path / "dataset.sqlite3")
    scenarios, states, _, _ = export.load_campaign(db, tmp_path)
    assert db.execute("SELECT frames_fully_sent,frames_fully_received,frame_counts_validated "
                      "FROM videos").fetchone() == (2, 2, 1)
    for fid in range(2):
        db.execute("INSERT INTO frames (campaign_id,scenario_id,attempt_id,video_id,"
                   "frame_id,ue_alias) VALUES (?,?,?,?,?,?)", ("c", "s", "good", "v", fid, "pixel1"))
    assert export.validate(db, scenarios, states)["status"] == "pass"
    events = tmp_path / "events.jsonl"
    write_jsonl(events, [dict(campaign_id="c", scenario_id="s", attempt_id=attempt,
                             type="descriptor", video_id=attempt, frame_id=0)
                        for attempt in ("bad", "good")])
    export.ingest_mec_events(db, [events], export.selected_attempts(states))
    assert db.execute("SELECT attempt_id FROM descriptors").fetchall() == [("good",)]
    db.close()
    # Exercise the actual CLI order: full-attempt validation, selection, SQLite,
    # CSV, and report output. Invalid attempts never enter the selection counts.
    jobs = tmp_path / "jobs.jsonl"
    records = [dict(campaign_id="c", scenario_id="s", attempt_id=attempt,
                    video_id="v", frame_id=fid, outcome="completed",
                    stage_times=dict(first_packet_monotonic_ns=arrival))
               for attempt, fid, arrival in (("good", 0, 11_000_000_000),
                                             ("good", 1, 16_000_000_000),
                                             ("bad", 0, 12_000_000_000))]
    write_jsonl(jobs, records)
    ran = tmp_path / "ran.jsonl"
    write_jsonl(ran, [])
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["export", "--campaign-dir", str(tmp_path),
                       "--mec-root", str(tmp_path), "--edgeric-root", str(ran),
                       "--output", str(output), "--strict"])
    original_jobs = jobs.read_bytes()
    export.main()
    report = json.loads((output / "validation.json").read_text())
    assert report["status"] == "pass"
    assert report["full_attempt_frame_rows"] == 2
    assert report["frame_rows"] == 1
    assert report["collection_window"]["outside_window"] == 1
    assert jobs.read_bytes() == original_jobs
    import csv
    with (output / "frames.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["attempt_id"], row["frame_id"]) for row in rows] == [("good", "0")]
    # A missing out-of-window job still fails full-attempt completeness.
    import pytest
    write_jsonl(jobs, records[:1])
    with pytest.raises(SystemExit) as error:
        export.main()
    assert error.value.code == 1
    report = json.loads((output / "validation.json").read_text())
    assert report["status"] == "fail"
    assert "expected 2" in report["problems"][0]



def test_middle_five_seconds_preserves_outcomes_and_prunes_related_rows(tmp_path):
    start = 10_000_000_000
    offsets = [999_999_999, 1_000_000_000, 5_999_999_999, 6_000_000_000, None]
    evidence = dict(release=dict(start_monotonic_ns=start),
                    videos={"v": dict(ue_alias="pixel1", counters={"completed": 3, "failed": 2},
                                      frame_counts_validated=True)})
    states = {"s": ("valid", 1, "a", 0, 0, None, json.dumps(evidence))}
    scenarios = {"s": dict(ues=[dict(ue_alias="pixel1", frame_count=70)])}
    db = export.create_database(tmp_path / "dataset.sqlite3")
    for fid, offset in enumerate(offsets):
        arrival = None if offset is None else start + offset
        db.execute("INSERT INTO frames (campaign_id,scenario_id,attempt_id,video_id,"
                   "frame_id,ue_alias,first_packet_monotonic_ns,outcome,result_ready_monotonic_ns) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ("c", "s", "a", "v", fid, "pixel1", arrival,
                    "failed" if fid in (2, 4) else "completed", start + 8_000_000_000))
        for table in ("descriptors", "ue_transmissions", "ue_receipts"):
            db.execute(f"INSERT INTO {table} (campaign_id,scenario_id,attempt_id,video_id,frame_id) "
                       "VALUES (?,?,?,?,?)", ("c", "s", "a", "v", fid))
        for table, link, key in (("ran_windows", "frame_ran_windows", "ran_window_id"),
                                 ("mec_resources", "frame_mec_windows", "mec_resource_id")):
            db.execute(f"INSERT INTO {table} (id,window_start_unix_ns,window_end_unix_ns) "
                       "VALUES (?,?,?)", (fid, 100, 200))
            db.execute(f"INSERT INTO {link} (campaign_id,scenario_id,attempt_id,video_id,"
                       f"frame_id,{key}) VALUES (?,?,?,?,?,?)", ("c", "s", "a", "v", fid, fid))
        db.execute("INSERT INTO media_events (scenario_id,attempt_id,monotonic_ns) VALUES (?,?,?)",
                   ("s", "a", arrival))
    # A retained frame shares a window with an excluded frame: keep that window.
    db.execute("INSERT INTO frame_ran_windows VALUES ('c','s','a','v',1,0,'test',1)")
    report = export.validate(db, scenarios, states)
    assert report["status"] == "pass"
    window = export.select_export_window(db, states)
    assert (window["exported"], window["outside_window"], window["unknown_window"]) == (2, 2, 1)
    assert window["attempts"][0]["unknown_window"] == 1
    assert db.execute("SELECT frame_id,outcome,result_ready_monotonic_ns FROM frames ORDER BY frame_id").fetchall() == [
        (1, "completed", start + 8_000_000_000), (2, "failed", start + 8_000_000_000)]
    for table in ("descriptors", "ue_transmissions", "ue_receipts", "frame_mec_windows"):
        assert db.execute(f"SELECT frame_id FROM {table} ORDER BY frame_id").fetchall() == [(1,), (2,)]
    assert db.execute("SELECT id,window_start_unix_ns,window_end_unix_ns FROM ran_windows ORDER BY id").fetchall() == [
        (0, 100, 200), (1, 100, 200), (2, 100, 200)]
    assert db.execute("SELECT id FROM mec_resources ORDER BY id").fetchall() == [(1,), (2,)]
    assert db.execute("SELECT count(*) FROM media_events").fetchone()[0] == 2
    assert export.frame_summary(db)["frame_rows"] == 2
    db.close()


def test_export_refuses_missing_scenario_clock(tmp_path):
    import pytest
    db = export.create_database(tmp_path / "dataset.sqlite3")
    states = {"s": ("valid", 1, "a", 0, 0, None, "{}")}
    with pytest.raises(ValueError, match="missing usable MEC start_monotonic_ns"):
        export.select_export_window(db, states)
    db.close()


def test_ran_join_uses_full_reception_or_last_packet_endpoint(tmp_path):
    db = export.create_database(tmp_path / 'dataset.sqlite3')
    ctx = dict(campaign_id='c', scenario_id='s', attempt_id='a')
    for idx, (start, end) in enumerate([(100, 200), (200, 300), (300, 400)]):
        db.execute('INSERT INTO ran_windows (id,campaign_id,scenario_id,attempt_id,ue_id,'
                   'window_start_unix_ns,window_end_unix_ns) VALUES (?,?,?,?,?,?,?)',
                   (idx, 'c', 's', 'a', 'u', start, end))
    stages = [dict(complete_reception_unix_ns=350, last_packet_reception_unix_ns=340),
              dict(last_packet_reception_unix_ns=250),
              dict(last_packet_reception_unix_ns=200), {},
              dict(last_packet_reception_unix_ns=90)]
    for fid in range(len(stages)):
        db.execute('INSERT INTO descriptors (campaign_id,scenario_id,attempt_id,video_id,'
                   'frame_id,event_unix_ns,tx_unix_ns) VALUES (?,?,?,?,?,?,?)',
                   ('c', 's', 'a', 'v', fid, 125, 105))
    jobs = tmp_path / 'jobs.jsonl'
    write_jsonl(jobs, [dict(ctx, video_id='v', frame_id=fid, stage_times=stage)
                       for fid, stage in enumerate(stages)])
    export.ingest_jobs(db, [jobs], {'s': dict(execution_index=0)},
                       {'s': ('valid', 1, 'a')}, {('s', 'a', 'v'): ('pixel1', 'u')}, {})
    assert db.execute('SELECT frame_id,count(*),sum(overlap_ns) FROM frame_ran_windows '
                      'GROUP BY frame_id ORDER BY frame_id').fetchall() == [
                          (0, 3, 245), (1, 2, 145), (2, 1, 95), (3, 1, 20)]
    assert db.execute('SELECT ran_join_basis,ran_join_end_unix_ns FROM frames ORDER BY frame_id').fetchall() == [
        ('ue_tx_to_mec_complete_reception', 350),
        ('ue_tx_to_mec_last_packet_reception', 250),
        ('ue_tx_to_mec_last_packet_reception', 200),
        ('ue_tx_to_mec_descriptor_arrival', 125),
        ('invalid_timestamp_order', 90)]
    db.close()
