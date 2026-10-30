#!/usr/bin/env python3
"""Join campaign, MEC, EdgeRIC, and optional UE evidence without relabeling it.

The durable SQLite file is the canonical export. CSV files provide convenient
flat views while JSON columns retain measurements whose internal validity and
layer/direction labels must not be discarded.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path


CONTEXT = ("campaign_id", "scenario_id", "attempt_id")


def json_lines(paths):
    for path in paths:
        with path.open(errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    yield path, number, json.loads(line)
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"invalid JSON at {path}:{number}: {error}")


def evidence_files(value, name):
    path = Path(value)
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)
    return sorted(path.rglob(name))


def flat_json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def context(record):
    return tuple(record.get(key) for key in CONTEXT)


def ue_kind(alias):
    """Return the device class encoded by the immutable campaign aliases."""
    if alias == "linux1":
        return "linux"
    if alias and alias.startswith("pixel"):
        return "android"
    return None


def selected_attempts(states):
    return {
        (scenario_id, state[2])
        for scenario_id, state in states.items()
        if state[0] == "valid" and state[2]
    }


def create_database(path):
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE scenarios (
          campaign_id TEXT, scenario_id TEXT PRIMARY KEY, execution_index INTEGER,
          status TEXT, attempts INTEGER, attempt_id TEXT, started_unix_ns INTEGER,
          finished_unix_ns INTEGER, error TEXT, ue_set TEXT, application_rotation INTEGER,
          tier INTEGER, radio_profile TEXT, carrier_prbs INTEGER,
          nominal_bandwidth_mhz REAL, center_frequency_hz REAL,
          subcarrier_spacing_hz REAL, threequarter_sampling INTEGER,
          fft_size INTEGER, sample_rate_hz REAL,
          channel_condition_id TEXT, channel_model TEXT, sinr_db REAL,
          delay_spread_ns REAL, speed_mps REAL, duration_s REAL, fps INTEGER,
          gop_frames INTEGER, scenario_json TEXT, runner_evidence_json TEXT);
        CREATE TABLE videos (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, video_id TEXT PRIMARY KEY,
          ue_alias TEXT, ue_id TEXT, ue_kind TEXT, application TEXT, candidate_id TEXT,
          bitrate_bps INTEGER, media_id TEXT, media_path TEXT, source_start_frame INTEGER,
          frame_count INTEGER, terminal_state TEXT, completed INTEGER, dropped INTEGER,
          failed INTEGER, collection_error TEXT);
        CREATE TABLE descriptors (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, video_id TEXT,
          frame_id INTEGER, event_unix_ns INTEGER, tx_unix_ns INTEGER,
          capture_unix_ns INTEGER, encoded_bytes INTEGER, frame_type INTEGER,
          gop_id INTEGER, packet_monotonic_ns INTEGER, payload_json TEXT,
          PRIMARY KEY (campaign_id,scenario_id,attempt_id,video_id,frame_id));
        CREATE TABLE ue_transmissions (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, ue_alias TEXT,
          video_id TEXT, frame_id INTEGER, capture_mono_ns INTEGER, tx_mono_ns INTEGER,
          encoded_bytes INTEGER, frame_type TEXT, payload_json TEXT,
          PRIMARY KEY (campaign_id,scenario_id,attempt_id,video_id,frame_id));
        CREATE TABLE ue_receipts (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, ue_alias TEXT,
          video_id TEXT, frame_id INTEGER, result_id TEXT, receipt_mono_ns INTEGER,
          latency_ns INTEGER, slo_success INTEGER, outcome TEXT, reason TEXT,
          payload_json TEXT,
          PRIMARY KEY (campaign_id,scenario_id,attempt_id,video_id,frame_id));
        CREATE TABLE ran_windows (
          id INTEGER PRIMARY KEY, campaign_id TEXT, scenario_id TEXT, attempt_id TEXT,
          ue_id TEXT, rnti INTEGER, sequence INTEGER, window_start_unix_ns INTEGER,
          window_end_unix_ns INTEGER, metric_age_s REAL, association_valid INTEGER,
          scheduler_ul_mcs REAL, scheduler_dl_mcs REAL, ul_bler REAL, dl_bler REAL,
          pusch_snr_db REAL, pucch_snr_db REAL, scheduled_prbs INTEGER,
          scheduled_tbs_bytes INTEGER, retransmission_prbs INTEGER,
          retransmission_tbs_bytes INTEGER, dl_scheduled_prbs INTEGER,
          dl_scheduled_tbs_bytes INTEGER, dl_retransmission_prbs INTEGER,
          dl_retransmission_tbs_bytes INTEGER, throughput_bytes_s REAL,
          throughput_valid INTEGER, radio_state_json TEXT, cumulative_grants_json TEXT,
          latest_ul_grant_json TEXT, latest_dl_grant_json TEXT, rlc_counters_json TEXT,
          capacity_validation_json TEXT, report_json TEXT);
        CREATE TABLE mec_resources (
          id INTEGER PRIMARY KEY, campaign_id TEXT, scenario_id TEXT, attempt_id TEXT,
          window_start_unix_ns INTEGER, window_end_unix_ns INTEGER,
          snapshot_monotonic_ns INTEGER, streams_json TEXT, workers_json TEXT,
          gpu_json TEXT, payload_json TEXT);
        CREATE TABLE media_events (
          id INTEGER PRIMARY KEY, campaign_id TEXT, scenario_id TEXT, attempt_id TEXT,
          video_id TEXT, event_type TEXT, monotonic_ns INTEGER, unix_ns INTEGER,
          payload_json TEXT);
        CREATE TABLE frames (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, execution_index INTEGER,
          ue_alias TEXT, ue_id TEXT, ue_kind TEXT, video_id TEXT, frame_id INTEGER,
          application TEXT, candidate_id TEXT, model TEXT, bitrate_bps INTEGER,
          cuda_priority INTEGER, gop_id INTEGER, config_version INTEGER,
          plan_version INTEGER, frame_type INTEGER, encoded_bytes INTEGER,
          outcome TEXT, reason TEXT, first_packet_monotonic_ns INTEGER,
          complete_reception_monotonic_ns INTEGER, decoded_monotonic_ns INTEGER,
          decode_s REAL, queue_wait_s REAL, preprocess_s REAL, inference_s REAL,
          cuda_event_ms REAL, postprocess_s REAL, processing_s REAL,
          result_prepare_s REAL, result_ready_monotonic_ns INTEGER,
          ue_capture_mono_ns INTEGER, ue_tx_mono_ns INTEGER,
          ue_receipt_mono_ns INTEGER, ue_latency_ns INTEGER, ue_slo_success INTEGER,
          ran_window_id INTEGER, ran_join_basis TEXT, ran_join_unix_ns INTEGER,
          stage_times_json TEXT, job_json TEXT,
          PRIMARY KEY (campaign_id,scenario_id,attempt_id,video_id,frame_id));
        CREATE TABLE frame_ran_windows (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, video_id TEXT,
          frame_id INTEGER, ran_window_id INTEGER, join_basis TEXT,
          overlap_ns INTEGER,
          PRIMARY KEY (campaign_id,scenario_id,attempt_id,video_id,frame_id,ran_window_id));
        CREATE TABLE frame_mec_windows (
          campaign_id TEXT, scenario_id TEXT, attempt_id TEXT, video_id TEXT,
          frame_id INTEGER, mec_resource_id INTEGER, overlap_ns INTEGER,
          PRIMARY KEY (campaign_id,scenario_id,attempt_id,video_id,frame_id,mec_resource_id));
        CREATE INDEX ran_lookup ON ran_windows(
          campaign_id,scenario_id,attempt_id,ue_id,window_start_unix_ns,window_end_unix_ns);
        CREATE INDEX frame_scenario ON frames(scenario_id,attempt_id,ue_alias,frame_id);
        CREATE VIEW frame_ran_features AS
          SELECT f.*, s.ue_set AS scenario_ue_set,
                 s.application_rotation AS scenario_application_rotation,
                 s.tier AS scenario_tier,
                 s.radio_profile, s.carrier_prbs, s.nominal_bandwidth_mhz,
                 s.center_frequency_hz, s.subcarrier_spacing_hz,
                 s.threequarter_sampling, s.fft_size, s.sample_rate_hz,
                 s.channel_condition_id, s.channel_model, s.sinr_db,
                 s.delay_spread_ns, s.speed_mps,
                 b.overlap_ns AS ran_overlap_ns,
                 r.window_start_unix_ns AS ran_window_start_unix_ns,
                 r.window_end_unix_ns AS ran_window_end_unix_ns,
                 r.rnti, r.association_valid, r.scheduler_ul_mcs,
                 r.scheduler_dl_mcs, r.ul_bler, r.dl_bler, r.pusch_snr_db,
                 r.pucch_snr_db, r.scheduled_prbs, r.scheduled_tbs_bytes,
                 r.retransmission_prbs, r.retransmission_tbs_bytes,
                 r.dl_scheduled_prbs, r.dl_scheduled_tbs_bytes,
                 r.dl_retransmission_prbs, r.dl_retransmission_tbs_bytes,
                 r.throughput_bytes_s, r.throughput_valid,
                 r.radio_state_json, r.cumulative_grants_json,
                 r.latest_ul_grant_json, r.latest_dl_grant_json,
                 r.rlc_counters_json, r.capacity_validation_json
          FROM frames f
          JOIN scenarios s USING (campaign_id,scenario_id)
          JOIN frame_ran_windows b USING (
            campaign_id,scenario_id,attempt_id,video_id,frame_id)
          JOIN ran_windows r ON r.id=b.ran_window_id;
        CREATE VIEW frame_mec_features AS
          SELECT f.*, s.ue_set AS scenario_ue_set,
                 s.application_rotation AS scenario_application_rotation,
                 s.tier AS scenario_tier,
                 s.radio_profile, s.carrier_prbs, s.nominal_bandwidth_mhz,
                 s.center_frequency_hz, s.subcarrier_spacing_hz,
                 s.threequarter_sampling, s.fft_size, s.sample_rate_hz,
                 s.channel_condition_id, s.channel_model, s.sinr_db,
                 s.delay_spread_ns, s.speed_mps,
                 b.overlap_ns AS mec_overlap_ns,
                 m.window_start_unix_ns AS mec_window_start_unix_ns,
                 m.window_end_unix_ns AS mec_window_end_unix_ns,
                 m.snapshot_monotonic_ns, m.streams_json, m.workers_json,
                 m.gpu_json
          FROM frames f
          JOIN scenarios s USING (campaign_id,scenario_id)
          JOIN frame_mec_windows b USING (
            campaign_id,scenario_id,attempt_id,video_id,frame_id)
          JOIN mec_resources m ON m.id=b.mec_resource_id;
        """
    )
    return db


def load_campaign(db, campaign_dir):
    scenarios_path = campaign_dir / "scenarios.jsonl"
    state_path = campaign_dir / "state.sqlite3"
    scenarios = {}
    for _, _, scenario in json_lines([scenarios_path]):
        scenarios[scenario["scenario_id"]] = scenario
    state = sqlite3.connect(state_path)
    states = {
        row[0]: row[1:]
        for row in state.execute(
            "SELECT scenario_id,status,attempts,attempt_id,started_unix_ns,"
            "finished_unix_ns,error,evidence_json FROM scenarios"
        )
    }
    video_map = {}
    assignments = {}
    rows = []
    for sid, scenario in scenarios.items():
        status, attempts, aid, started, finished, error, evidence_text = states.get(
            sid, ("pending", 0, None, None, None, None, None)
        )
        evidence = json.loads(evidence_text) if evidence_text else {}
        factors, channel = scenario["factors"], scenario["channel"]
        radio = scenario.get("radio", {})
        rows.append(
            (
                scenario["campaign_id"], sid, scenario["execution_index"], status,
                attempts, aid, started, finished, error, factors["ue_set"],
                factors["application_rotation"], factors["tier"],
                radio.get("profile_id"), radio.get("carrier_prbs"),
                radio.get("nominal_bandwidth_mhz"),
                radio.get("center_frequency_hz"),
                radio.get("subcarrier_spacing_hz"),
                radio.get("threequarter_sampling"), radio.get("fft_size"),
                radio.get("sample_rate_hz"),
                channel["condition_id"], channel["model"], channel.get("sinr_db"),
                channel.get("delay_spread_ns"), channel.get("speed_mps"),
                scenario["duration_s"], scenario["fps"], scenario["gop_frames"],
                flat_json(scenario), flat_json(evidence),
            )
        )
        by_alias = {item["ue_alias"]: item for item in scenario["ues"]}
        assignments[sid] = by_alias
        if status != "valid":
            continue
        for vid, item in evidence.get("videos", {}).items():
            alias = item.get("ue_alias")
            assignment = by_alias.get(alias, {})
            counters = item.get("counters", {})
            video_map[(sid, aid, vid)] = (alias, item.get("ue_id"))
            db.execute(
                "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scenario["campaign_id"], sid, aid, vid, alias, item.get("ue_id"),
                    ue_kind(alias), item.get("application"),
                    item.get("candidate_id"), assignment.get("bitrate_bps"),
                    assignment.get("media_id"), assignment.get("media_path"),
                    assignment.get("source_start_frame"), assignment.get("frame_count"),
                    item.get("state"), counters.get("completed", 0),
                    counters.get("dropped", 0), counters.get("failed", 0),
                    item.get("collection_error"),
                ),
            )
    db.executemany("INSERT INTO scenarios VALUES (" + ",".join("?" * 30) + ")", rows)
    db.commit()
    return scenarios, states, video_map, assignments


def ingest_mec_events(db, paths, selected=None):
    descriptors = 0
    resources = 0
    media = 0
    media_types = {
        "packet_loss", "media_error", "pipeline", "input_closed",
        "receiver_setup_failed", "collection_run_invalid",
    }
    for _, _, event in json_lines(paths):
        c, sid, aid = context(event)
        kind = event.get("type")
        if not all((c, sid, aid)):
            continue
        if selected is not None and (sid, aid) not in selected:
            continue
        if kind == "descriptor" and event.get("frame_id") is not None:
            db.execute(
                "INSERT OR REPLACE INTO descriptors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    c, sid, aid, event.get("video_id"), event["frame_id"],
                    event.get("unix_ns"), event.get("tx_unix_ns"),
                    event.get("capture_unix_ns"), event.get("encoded_bytes"),
                    event.get("frame_type"), event.get("gop_id"),
                    event.get("monotonic_ns"), flat_json(event),
                ),
            )
            descriptors += 1
        elif kind == "mec_resources":
            snap = event.get("snapshot", {})
            row = (
                c, sid, aid, snap.get("window_start_unix_ns"),
                snap.get("window_end_unix_ns"), snap.get("snapshot_monotonic_ns"),
                flat_json(snap.get("streams")), flat_json(snap.get("workers")),
                flat_json(snap.get("gpu")), flat_json(event),
            )
            db.execute(
                "INSERT INTO mec_resources VALUES (NULL," +
                ",".join("?" * len(row)) + ")", row,
            )
            resources += 1
        elif kind in media_types:
            db.execute(
                "INSERT INTO media_events VALUES (NULL,?,?,?,?,?,?,?,?)",
                (
                    c, sid, aid, event.get("video_id"), kind,
                    event.get("monotonic_ns"), event.get("unix_ns"), flat_json(event),
                ),
            )
            media += 1
    db.commit()
    return dict(descriptors=descriptors, mec_resources=resources, media_events=media)


def ingest_ran(db, paths, selected=None):
    count = 0
    for _, _, event in json_lines(paths):
        if event.get("type") != "radio_report":
            continue
        report = event.get("report", {})
        c, sid, aid = context(report)
        if not all((c, sid, aid)):
            continue
        if selected is not None and (sid, aid) not in selected:
            continue
        for uid, ue in report.get("ues", {}).items():
            radio = ue.get("radio_state") or {}
            row = (
                c, sid, aid, uid, ue.get("rnti"), report.get("sequence"),
                report.get("window_start_unix_ns"), report.get("window_end_unix_ns"),
                ue.get("metric_age_s"), bool(ue.get("association_valid")),
                radio.get("scheduler_ul_mcs"), radio.get("scheduler_dl_mcs"),
                radio.get("ul_bler"), radio.get("dl_bler"),
                radio.get("pusch_snr_db"), radio.get("pucch_snr_db"),
                ue.get("scheduled_prbs"), ue.get("scheduled_tbs_bytes"),
                ue.get("retransmission_prbs"), ue.get("retransmission_tbs_bytes"),
                ue.get("dl_scheduled_prbs"), ue.get("dl_scheduled_tbs_bytes"),
                ue.get("dl_retransmission_prbs"),
                ue.get("dl_retransmission_tbs_bytes"), ue.get("throughput_bytes_s"),
                bool(ue.get("throughput_valid")), flat_json(radio),
                flat_json(ue.get("cumulative_grants")),
                flat_json(ue.get("latest_ul_grant")),
                flat_json(ue.get("latest_dl_grant")),
                flat_json(ue.get("rlc_counters")),
                flat_json(ue.get("capacity_validation")), flat_json(report),
            )
            db.execute(
                "INSERT INTO ran_windows VALUES (NULL," +
                ",".join("?" * len(row)) + ")", row,
            )
            count += 1
    db.commit()
    return count


def parse_ue_argument(item):
    if "=" not in item:
        raise ValueError("--ue-log must be ALIAS=PATH")
    alias, path = item.split("=", 1)
    if not alias or not path:
        raise ValueError("--ue-log must be ALIAS=PATH")
    return alias, Path(path)


def ingest_ue(db, arguments, selected=None):
    counts = Counter()
    for item in arguments:
        alias, path = parse_ue_argument(item)
        for _, _, event in json_lines(evidence_files(path, "events.jsonl")):
            c, sid, aid = context(event)
            if not all((c, sid, aid)) or event.get("frame_id") is None:
                continue
            if selected is not None and (sid, aid) not in selected:
                continue
            common = (c, sid, aid, alias, event.get("video_id"), event["frame_id"])
            if event.get("type") == "transmitted":
                db.execute(
                    "INSERT OR REPLACE INTO ue_transmissions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    common
                    + (
                        event.get("capture_mono_ns"), event.get("tx_mono_ns"),
                        event.get("encoded_bytes"), event.get("frame_type"),
                        flat_json(event),
                    ),
                )
                counts["transmitted"] += 1
            elif event.get("type") == "result_receipt":
                db.execute(
                    "INSERT OR REPLACE INTO ue_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    common
                    + (
                        event.get("result_id"), event.get("receipt_mono_ns"),
                        event.get("latency_ns"), bool(event.get("slo_success")),
                        event.get("outcome"), event.get("reason"), flat_json(event),
                    ),
                )
                counts["result_receipt"] += 1
    db.commit()
    return dict(counts)


def radio_windows(db, c, sid, aid, uid, start, end):
    if start is None or uid is None:
        return []
    if end is None or end < start:
        end = start
    if end == start:
        return db.execute(
            "SELECT id,window_start_unix_ns,window_end_unix_ns FROM ran_windows "
            "WHERE campaign_id=? AND scenario_id=? AND attempt_id=? AND ue_id=? "
            "AND window_start_unix_ns<=? AND window_end_unix_ns>? "
            "ORDER BY window_start_unix_ns",
            (c, sid, aid, uid, start, start),
        ).fetchall()
    return db.execute(
        "SELECT id,window_start_unix_ns,window_end_unix_ns FROM ran_windows "
        "WHERE campaign_id=? AND scenario_id=? AND attempt_id=? AND ue_id=? "
        "AND window_end_unix_ns>? AND window_start_unix_ns<? "
        "ORDER BY window_start_unix_ns",
        (c, sid, aid, uid, start, end),
    ).fetchall()


def mec_windows(db, c, sid, aid, start_mono, end_mono):
    if start_mono is None or end_mono is None or end_mono < start_mono:
        return []
    # snapshot_monotonic_ns is the interval end. Its duration is copied from
    # the corresponding Unix interval, so no wall/monotonic clock conversion
    # is needed for this overlap.
    return db.execute(
        "SELECT id,snapshot_monotonic_ns-(window_end_unix_ns-window_start_unix_ns),"
        "snapshot_monotonic_ns FROM mec_resources WHERE campaign_id=? "
        "AND scenario_id=? AND attempt_id=? AND snapshot_monotonic_ns>? "
        "AND snapshot_monotonic_ns-(window_end_unix_ns-window_start_unix_ns)<? "
        "ORDER BY snapshot_monotonic_ns",
        (c, sid, aid, start_mono, end_mono),
    ).fetchall()


def ingest_jobs(db, paths, scenarios, states, video_map, assignments):
    count = 0
    duplicates = 0
    for _, _, job in json_lines(paths):
        c, sid, aid = context(job)
        if not all((c, sid, aid)) or sid not in scenarios:
            continue
        state = states.get(sid)
        # The canonical dataset contains the latest valid attempt. Interrupted
        # and invalid attempts remain available in the immutable raw logs.
        if not state or state[0] != "valid" or aid != state[2]:
            continue
        vid, fid = job.get("video_id"), job.get("frame_id")
        if vid is None or fid is None:
            continue
        alias, uid = video_map.get((sid, aid, vid), (None, job.get("ue_id")))
        assignment = assignments.get(sid, {}).get(alias, {})
        descriptor = db.execute(
            "SELECT event_unix_ns,tx_unix_ns,encoded_bytes,frame_type,payload_json "
            "FROM descriptors WHERE campaign_id=? AND scenario_id=? AND attempt_id=? "
            "AND video_id=? AND frame_id=?",
            (c, sid, aid, vid, fid),
        ).fetchone()
        event_unix = descriptor[0] if descriptor else None
        tx_unix = descriptor[1] if descriptor else None
        join_unix = tx_unix or event_unix
        ran_end = event_unix if tx_unix is not None else join_unix
        basis = "ue_tx_to_mec_descriptor_arrival" if tx_unix else (
            "mec_descriptor_arrival_unix_ns" if event_unix else None
        )
        ran = radio_windows(db, c, sid, aid, uid, join_unix, ran_end)
        tx = db.execute(
            "SELECT capture_mono_ns,tx_mono_ns FROM ue_transmissions WHERE "
            "campaign_id=? AND scenario_id=? AND attempt_id=? AND video_id=? AND frame_id=?",
            (c, sid, aid, vid, fid),
        ).fetchone() or (None, None)
        receipt = db.execute(
            "SELECT receipt_mono_ns,latency_ns,slo_success FROM ue_receipts WHERE "
            "campaign_id=? AND scenario_id=? AND attempt_id=? AND video_id=? AND frame_id=?",
            (c, sid, aid, vid, fid),
        ).fetchone() or (None, None, None)
        stage = job.get("stage_times") or {}
        scenario = scenarios[sid]
        row = (
            c, sid, aid, scenario["execution_index"], alias, uid,
            ue_kind(alias), vid, fid, job.get("application"),
            job.get("candidate_id"), job.get("model"), job.get("bitrate_bps"),
            job.get("priority"), job.get("gop_id"), job.get("config_version"),
            job.get("plan_version"), descriptor[3] if descriptor else None,
            descriptor[2] if descriptor else None, job.get("outcome"), job.get("reason"),
            stage.get("first_packet_monotonic_ns"),
            stage.get("complete_reception_monotonic_ns"),
            stage.get("decoded_monotonic_ns"), stage.get("decode_s"),
            stage.get("queue_wait_s"), stage.get("preprocess_s"),
            stage.get("inference_s"), stage.get("cuda_event_ms"),
            stage.get("postprocess_s"), stage.get("processing_s"),
            stage.get("result_prepare_s"), stage.get("result_ready_monotonic_ns"),
            tx[0], tx[1], receipt[0], receipt[1], receipt[2],
            ran[0][0] if ran else None, basis, join_unix,
            flat_json(stage), flat_json(job),
        )
        try:
            db.execute(
                "INSERT INTO frames VALUES (" + ",".join("?" * len(row)) + ")", row
            )
            count += 1
            for ran_id, window_start, window_end in ran:
                overlap = 0
                if ran_end is not None and ran_end > join_unix:
                    overlap = max(
                        0,
                        min(ran_end, window_end) - max(join_unix, window_start),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO frame_ran_windows VALUES (?,?,?,?,?,?,?,?)",
                    (c, sid, aid, vid, fid, ran_id, basis, overlap),
                )
            processing_start = stage.get("first_packet_monotonic_ns")
            processing_end = stage.get("result_ready_monotonic_ns")
            for resource_id, window_start, window_end in mec_windows(
                db, c, sid, aid, processing_start, processing_end
            ):
                overlap = max(
                    0,
                    min(processing_end, window_end)
                    - max(processing_start, window_start),
                )
                db.execute(
                    "INSERT OR IGNORE INTO frame_mec_windows VALUES (?,?,?,?,?,?,?)",
                    (c, sid, aid, vid, fid, resource_id, overlap),
                )
        except sqlite3.IntegrityError:
            duplicates += 1
    db.commit()
    return dict(frames=count, duplicate_frames=duplicates)


def export_table(db, table, path):
    cursor = db.execute(f"SELECT * FROM {table}")
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([item[0] for item in cursor.description])
        writer.writerows(cursor)


def validate(db, scenarios, states):
    problems = []
    valid = 0
    for sid, scenario in scenarios.items():
        state = states.get(sid)
        if not state or state[0] != "valid":
            continue
        valid += 1
        aid = state[2]
        for assignment in scenario["ues"]:
            alias = assignment["ue_alias"]
            actual = db.execute(
                "SELECT count(*) FROM frames WHERE scenario_id=? AND attempt_id=? "
                "AND ue_alias=?", (sid, aid, alias),
            ).fetchone()[0]
            if actual != assignment["frame_count"]:
                problems.append(
                    f"{sid}/{aid}/{alias}: {actual} frame rows; "
                    f"expected {assignment['frame_count']}"
                )
    frame_count = db.execute("SELECT count(*) FROM frames").fetchone()[0]
    ran_joined = db.execute(
        "SELECT count(*) FROM frames WHERE ran_window_id IS NOT NULL"
    ).fetchone()[0]
    mec_joined = db.execute(
        "SELECT count(*) FROM (SELECT DISTINCT campaign_id,scenario_id,attempt_id,"
        "video_id,frame_id FROM frame_mec_windows)"
    ).fetchone()[0]
    join_basis = dict(db.execute(
        "SELECT coalesce(ran_join_basis,'unjoined'),count(*) FROM frames GROUP BY 1"
    ).fetchall())
    return {
        "valid_scenarios": valid,
        "frame_rows": frame_count,
        "ran_joined_frames": ran_joined,
        "ran_join_fraction": ran_joined / frame_count if frame_count else None,
        "mec_joined_frames": mec_joined,
        "mec_join_fraction": mec_joined / frame_count if frame_count else None,
        "ran_join_basis": join_basis,
        "problems": problems,
        "status": "pass" if not problems else "fail",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--mec-root", required=True)
    parser.add_argument("--edgeric-root", required=True)
    parser.add_argument("--ue-log", action="append", default=[], metavar="ALIAS=PATH")
    parser.add_argument("--output", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    campaign_dir = Path(args.campaign_dir)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    db = create_database(output / "dataset.sqlite3")
    scenarios, states, video_map, assignments = load_campaign(db, campaign_dir)
    selected = selected_attempts(states)
    mec_events = evidence_files(args.mec_root, "events.jsonl")
    mec_jobs = evidence_files(args.mec_root, "jobs.jsonl")
    ran_events = evidence_files(args.edgeric_root, "events.jsonl")
    counts = {
        "mec": ingest_mec_events(db, mec_events, selected),
        "ran_windows": ingest_ran(db, ran_events, selected),
        "ue": ingest_ue(db, args.ue_log, selected),
    }
    counts["jobs"] = ingest_jobs(
        db, mec_jobs, scenarios, states, video_map, assignments
    )
    report = validate(db, scenarios, states)
    report["counts"] = counts
    ue_files = [
        path
        for item in args.ue_log
        for path in evidence_files(parse_ue_argument(item)[1], "events.jsonl")
    ]
    campaign_inputs = [campaign_dir / "scenarios.jsonl", campaign_dir / "state.sqlite3"]
    lock = campaign_dir / "manifest.lock.json"
    if lock.exists():
        campaign_inputs.append(lock)
    inputs = sorted(set(campaign_inputs + mec_events + mec_jobs + ran_events + ue_files))
    report["inputs"] = [
        {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in inputs
    ]
    (output / "validation.json").write_text(json.dumps(report, indent=2))
    for table in (
        "scenarios", "videos", "frames", "frame_ran_features",
        "frame_mec_features", "ran_windows", "mec_resources",
    ):
        export_table(db, table, output / f"{table}.csv")
    db.commit()
    db.close()
    print(json.dumps(report, indent=2))
    if args.strict and report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
