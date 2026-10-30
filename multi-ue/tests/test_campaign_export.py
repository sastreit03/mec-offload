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
            "gpu": {}}},
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
        "INSERT INTO scenarios VALUES (" + ",".join("?" * 30) + ")",
        (
            campaign, scenario_id, 1, "valid", 1, attempt, 1, 2, None,
            "one_ue", 0, 0, "band78-51prb-threequarter", 51, 20,
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
    db.close()
