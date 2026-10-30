#!/usr/bin/env python3
"""Checkpointed Aetheredge campaign runner.

The immutable JSONL defines work. SQLite only records attempts and validation,
so restarting this process never regenerates or repeats a valid scenario.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from channel_preflight import request as channel_request
from channel_trace import validate_trace_folder


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "EXPIRED", "DENIED"}


class RunnerStopped(Exception):
    """Operator interruption; preserve the checkpoint without recovery."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def http_json(base, path, value=None, timeout=5):
    data = None if value is None else json.dumps(value).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={"content-type": "application/json"},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class State:
    def __init__(self, path, manifest_hash):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS scenarios (
              scenario_id TEXT PRIMARY KEY, execution_index INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              attempt_id TEXT, started_unix_ns INTEGER, finished_unix_ns INTEGER,
              error TEXT, evidence_json TEXT)"""
        )
        prior = self.db.execute(
            "SELECT value FROM metadata WHERE key='scenarios_sha256'"
        ).fetchone()
        if prior and prior[0] != manifest_hash:
            raise RuntimeError("checkpoint belongs to a different scenarios.jsonl")
        self.db.execute(
            "INSERT OR IGNORE INTO metadata VALUES ('scenarios_sha256', ?)",
            (manifest_hash,),
        )
        # A process death leaves an explicit retryable checkpoint.
        self.db.execute(
            "UPDATE scenarios SET status='interrupted', error='runner interrupted' "
            "WHERE status='running'"
        )
        self.db.commit()

    def register(self, scenario):
        self.db.execute(
            "INSERT OR IGNORE INTO scenarios(scenario_id,execution_index) VALUES (?,?)",
            (scenario["scenario_id"], scenario["execution_index"]),
        )
        self.db.commit()

    def row(self, scenario_id):
        return self.db.execute(
            "SELECT status,attempts FROM scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()

    def start(self, scenario, attempt_id):
        self.db.execute(
            """UPDATE scenarios SET status='running', attempts=attempts+1,
               attempt_id=?, started_unix_ns=?, finished_unix_ns=NULL,
               error=NULL, evidence_json=NULL WHERE scenario_id=?""",
            (attempt_id, time.time_ns(), scenario["scenario_id"]),
        )
        self.db.commit()

    def finish(self, scenario_id, status, error=None, evidence=None):
        self.db.execute(
            """UPDATE scenarios SET status=?, finished_unix_ns=?, error=?,
               evidence_json=? WHERE scenario_id=?""",
            (
                status,
                time.time_ns(),
                error,
                json.dumps(evidence, sort_keys=True) if evidence else None,
                scenario_id,
            ),
        )
        self.db.commit()

    def counts(self):
        return dict(self.db.execute("SELECT status,count(*) FROM scenarios GROUP BY status"))


class Runner:
    def __init__(self, args, scenarios, state):
        self.args = args
        self.scenarios = scenarios
        self.state = state
        self.stop = False
        self.events = Path(args.output) / "runner-events.jsonl"
        self.heartbeat = Path(args.output) / "runner-status.json"
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, *_):
        self.stop = True

    def event(self, kind, **value):
        item = dict(type=kind, unix_ns=time.time_ns(), **value)
        with self.events.open("a") as stream:
            stream.write(json.dumps(item, separators=(",", ":")) + "\n")
        self.heartbeat.write_text(
            json.dumps(dict(last_event=item, counts=self.state.counts()), indent=2)
        )

    def recover(self, reason, scenario, attempt_id):
        self.event(
            "recovery", reason=reason, scenario_id=scenario["scenario_id"],
            attempt_id=attempt_id,
        )
        for hook in self.args.recovery_hook:
            try:
                subprocess.run(
                    [hook, reason, scenario["scenario_id"], attempt_id],
                    check=False,
                    timeout=self.args.recovery_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                self.event("recovery_hook_failed", hook=hook, reason=str(error))
        time.sleep(self.args.retry_delay)

    def preflight(self, scenario):
        status = http_json(self.args.mec, "/api/status")
        if not status["readiness"]["ready"]:
            raise RuntimeError("MEC is not ready")
        if status["readiness"].get("mode") != "collection":
            raise RuntimeError("MEC is not in collection mode")
        expected_priority = scenario["policies"]["cuda_priority"]
        if any(
            worker.get("streams") != [expected_priority]
            for worker in status["readiness"].get("workers", {}).values()
        ):
            raise RuntimeError("MEC CUDA stream priority differs from manifest")
        if status["unresolved"]:
            raise RuntimeError("MEC has unresolved frames")
        connected = {
            value["alias"] for value in status.get("ues", {}).values()
            if value.get("connected") and value.get("alias")
        }
        expected = {item["ue_alias"] for item in scenario["ues"]}
        if not expected <= connected:
            raise RuntimeError(f"UE aliases unavailable: {sorted(expected - connected)}")
        valid = {
            value["alias"] for value in status.get("ues", {}).values()
            if value.get("association_valid") and value.get("alias")
        }
        if not expected <= valid:
            raise RuntimeError(
                f"EdgeRIC associations unavailable: {sorted(expected - valid)}"
            )
        age = status.get("radio_report_age_s")
        if age is None or age > self.args.max_radio_age:
            raise RuntimeError(f"EdgeRIC radio report is stale: {age}")
        return status

    def await_preflight(self, scenario):
        """Wait for infrastructure and UEs without consuming a data attempt."""
        deadline = time.monotonic() + self.args.preflight_timeout
        last_error = None
        reported = None
        while not self.stop:
            try:
                status = self.preflight(scenario)
                if reported is not None:
                    print("Campaign preflight ready", flush=True)
                return status
            except Exception as error:
                last_error = error
                if str(error) != reported:
                    reported = str(error)
                    print(f"Waiting for campaign preflight: {error}", flush=True)
                    self.event(
                        "preflight_wait",
                        scenario_id=scenario["scenario_id"],
                        reason=str(error),
                    )
                if time.monotonic() >= deadline:
                    self.recover(
                        f"preflight unavailable: {error}", scenario, "preflight"
                    )
                    deadline = time.monotonic() + self.args.preflight_timeout
                time.sleep(self.args.poll_interval)
        raise RunnerStopped(str(last_error or "runner stopped"))

    def disarm(self, scenario, attempt_id):
        payload = {
            "campaign_id": scenario["campaign_id"],
            "scenario_id": scenario["scenario_id"],
            "attempt_id": attempt_id,
        }
        response = http_json(self.args.mec, "/api/scenario/disarm", payload)
        if response.get("status") not in ("disarmed", "already_disarmed"):
            raise RuntimeError(f"scenario disarm failed: {response}")

    def select_channel(self, scenario):
        condition = scenario["channel"]["condition_id"]
        record = validate_trace_folder(
            Path(self.args.trace_bank) / condition, condition_id=condition
        )
        response = channel_request(
            self.args.channel_endpoint,
            dict(
                msg_type="trace_select",
                condition_id=condition,
                trace_sha256=record["trace_sha256"],
            ),
        )
        if response.get("condition_id") != condition:
            raise RuntimeError("channel loader selected the wrong condition")
        if response.get("trace_sha256") != record["trace_sha256"]:
            raise RuntimeError("channel loader trace hash mismatch")
        return response

    def await_assignment_ack(self, scenario, attempt_id):
        """Require fresh bidirectional control proof for every assigned UE."""
        expected = {item["ue_alias"] for item in scenario["ues"]}
        context = {
            "campaign_id": scenario["campaign_id"],
            "scenario_id": scenario["scenario_id"],
            "attempt_id": attempt_id,
        }
        deadline = time.monotonic() + self.args.assignment_timeout
        missing = set(expected)
        while time.monotonic() < deadline and not self.stop:
            status = http_json(self.args.mec, "/api/status")
            if status.get("scenario") != context:
                raise RuntimeError("MEC scenario context changed before UE acknowledgement")
            acknowledged = {
                value.get("alias")
                for value in status.get("ues", {}).values()
                if value.get("connected")
                and value.get("scenario_acknowledged")
                and value.get("alias")
            }
            missing = expected - acknowledged
            if not missing:
                self.event(
                    "assignments_acknowledged",
                    scenario_id=scenario["scenario_id"],
                    attempt_id=attempt_id,
                    ue_aliases=sorted(acknowledged),
                )
                return status
            time.sleep(self.args.poll_interval)
        if self.stop:
            raise RunnerStopped("runner stopped")
        raise RuntimeError(
            f"UE assignment acknowledgement timed out: {sorted(missing)}"
        )

    def wait(self, scenario, attempt_id, expected_channel=None):
        expected = {item["ue_alias"]: item for item in scenario["ues"]}
        deadline = time.monotonic() + self.args.scenario_timeout
        progress_at = time.monotonic()
        progress = None
        last = None
        channel_check_at = time.monotonic()
        while time.monotonic() < deadline and not self.stop:
            if expected_channel is not None and time.monotonic() >= channel_check_at:
                try:
                    current_channel = channel_request(
                        self.args.channel_endpoint, {"msg_type": "trace_status"}
                    )
                except Exception as error:
                    raise RuntimeError(
                        f"gNB channel controller unavailable: {error}"
                    ) from error
                for key in ("condition_id", "trace_sha256", "generation"):
                    if current_channel.get(key) != expected_channel.get(key):
                        raise RuntimeError(f"channel {key} changed during scenario")
                channel_check_at = time.monotonic() + 5
            last = http_json(self.args.mec, "/api/status")
            if last.get("scenario") != {
                "campaign_id": scenario["campaign_id"],
                "scenario_id": scenario["scenario_id"],
                "attempt_id": attempt_id,
            }:
                raise RuntimeError("MEC scenario context changed")
            connected = {
                value.get("alias")
                for value in last.get("ues", {}).values()
                if value.get("connected") and value.get("alias")
            }
            missing = set(expected) - connected
            if missing:
                raise RuntimeError(
                    f"UE disconnected during scenario: {sorted(missing)}"
                )
            videos = {
                value.get("ue_alias"): value for value in last["videos"].values()
                if value.get("ue_alias") in expected
            }
            # Aetheredge: detect a dead UE media path without waiting for the
            # broader scenario timeout. Any terminal count, state, or MEC
            # unresolved-frame change resets this watchdog.
            current_progress = (
                last["unresolved"],
                tuple(
                    sorted(
                        (
                            alias,
                            value["state"],
                            tuple(sorted(value["counters"].items())),
                            value.get("final_frame_id"),
                            value.get("collection_error"),
                        )
                        for alias, value in videos.items()
                    )
                ),
            )
            if current_progress != progress:
                progress = current_progress
                progress_at = time.monotonic()
            if len(videos) == len(expected) and all(
                value["state"] in TERMINAL for value in videos.values()
            ):
                errors = []
                for alias, assignment in expected.items():
                    video = videos[alias]
                    outcomes = sum(video["counters"].values())
                    if video["candidate_id"] != assignment["candidate_id"]:
                        errors.append(f"{alias}: candidate mismatch")
                    if video["final_frame_id"] != assignment["frame_count"] - 1:
                        errors.append(f"{alias}: final frame mismatch")
                    if outcomes != assignment["frame_count"]:
                        errors.append(f"{alias}: {outcomes} terminal frame results")
                    if video.get("collection_error"):
                        errors.append(f"{alias}: {video['collection_error']}")
                if last["unresolved"]:
                    errors.append(f"{last['unresolved']} unresolved frames")
                if errors:
                    raise RuntimeError("; ".join(errors))
                return last
            stalled_for = time.monotonic() - progress_at
            if stalled_for >= self.args.stall_timeout:
                raise RuntimeError(
                    f"scenario stalled for {stalled_for:.1f} seconds"
                )
            self.event(
                "monitor", scenario_id=scenario["scenario_id"], attempt_id=attempt_id,
                videos={
                    alias: dict(
                        state=value["state"], counters=value["counters"]
                    )
                    for alias, value in videos.items()
                },
                unresolved=last["unresolved"],
            )
            time.sleep(self.args.poll_interval)
        if self.stop:
            raise RunnerStopped("runner stopped")
        raise RuntimeError("scenario timed out")

    def attempt(self, scenario, attempt_id, before):
        channel = self.select_channel(scenario)
        payload = {
            key: scenario[key]
            for key in ("campaign_id", "scenario_id", "ues")
        }
        payload["attempt_id"] = attempt_id
        armed = http_json(self.args.mec, "/api/scenario/arm", payload)
        if armed.get("status") not in ("armed", "already_armed"):
            raise RuntimeError(f"scenario arm failed: {armed}")
        self.await_assignment_ack(scenario, attempt_id)
        started = http_json(
            self.args.mec,
            "/api/scenario/start",
            {
                "campaign_id": scenario["campaign_id"],
                "scenario_id": scenario["scenario_id"],
                "attempt_id": attempt_id,
                "lead_s": self.args.start_lead_s,
            },
        )
        if started.get("status") != "started":
            raise RuntimeError(f"scenario start failed: {started}")
        self.event(
            "scenario_start_released",
            scenario_id=scenario["scenario_id"],
            attempt_id=attempt_id,
            start_unix_ns=started["start_unix_ns"],
            release_delay_ns=started["release_delay_ns"],
        )
        after = self.wait(scenario, attempt_id, channel)
        channel_after = channel_request(
            self.args.channel_endpoint, {"msg_type": "trace_status"}
        )
        for key in ("condition_id", "trace_sha256", "generation"):
            if channel_after.get(key) != channel.get(key):
                raise RuntimeError(f"channel {key} changed during scenario")
        return dict(
            mec_generation=before["awareness_generation"],
            channel=channel_after,
            videos=after["videos"],
        )

    def run(self):
        last_attempt = None
        for scenario in self.scenarios:
            self.state.register(scenario)
        for scenario in self.scenarios:
            if self.stop:
                break
            status, attempts = self.state.row(scenario["scenario_id"])
            if status == "valid":
                continue
            while attempts < self.args.max_attempts and not self.stop:
                try:
                    before = self.await_preflight(scenario)
                except RunnerStopped:
                    return
                attempt_id = "attempt-" + uuid.uuid4().hex
                last_attempt = (scenario, attempt_id)
                self.state.start(scenario, attempt_id)
                attempts += 1
                self.event(
                    "attempt_started", scenario_id=scenario["scenario_id"],
                    attempt_id=attempt_id, attempt_number=attempts,
                )
                try:
                    evidence = self.attempt(scenario, attempt_id, before)
                except RunnerStopped as error:
                    self.state.finish(
                        scenario["scenario_id"], "interrupted", str(error)
                    )
                    self.event(
                        "runner_stopped",
                        scenario_id=scenario["scenario_id"],
                        attempt_id=attempt_id,
                    )
                    # An armed MEC keeps advertising this assignment to UEs that
                    # reconnect after Ctrl-C. Recovery clears that stale attempt
                    # while the checkpoint remains interrupted and resumable.
                    self.recover(str(error), scenario, attempt_id)
                    return
                except Exception as error:
                    self.state.finish(scenario["scenario_id"], "retryable", str(error))
                    self.recover(str(error), scenario, attempt_id)
                    continue
                self.state.finish(scenario["scenario_id"], "valid", evidence=evidence)
                self.event(
                    "scenario_valid", scenario_id=scenario["scenario_id"],
                    attempt_id=attempt_id,
                )
                break
            if attempts >= self.args.max_attempts and self.state.row(
                scenario["scenario_id"]
            )[0] != "valid":
                self.state.finish(
                    scenario["scenario_id"], "failed", "maximum attempts exhausted"
                )
                self.event("scenario_failed", scenario_id=scenario["scenario_id"])
        if self.scenarios and not self.stop:
            # A pilot invoked with --scenario-id otherwise leaves its final
            # assignment armed, causing a UE that reconnects later to restart it.
            scenario, attempt_id = last_attempt or (
                self.scenarios[-1], "no-active-attempt"
            )
            self.event(
                "runner_completed",
                scenario_id=scenario["scenario_id"],
                attempt_id=attempt_id,
            )
            try:
                self.disarm(scenario, attempt_id)
            except Exception as error:
                # Fall back to restart only when the lightweight cleanup fails.
                self.recover(
                    f"runner completed cleanup failed: {error}",
                    scenario,
                    attempt_id,
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-dir", required=True)
    # The runner uses the MEC's public/control-side address; UEs still use
    # 192.168.72.136 through the cellular user plane.
    parser.add_argument("--mec", default="http://192.168.71.142:8765")
    parser.add_argument("--channel-endpoint", default="tcp://192.168.73.140:5560")
    parser.add_argument("--trace-bank", required=True)
    parser.add_argument("--scenario-id", help="run only one immutable scenario")
    parser.add_argument("--limit", type=int, help="stop after this many manifest rows")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--scenario-timeout", type=float, default=180)
    parser.add_argument(
        "--start-lead-s", type=float, default=6.0,
        help="admission/setup lead before the shared UE media release",
    )
    parser.add_argument(
        "--assignment-timeout", type=float, default=20,
        help="recover unless every assigned UE acknowledges its context",
    )
    parser.add_argument(
        "--stall-timeout", type=float, default=30,
        help="recover after this many seconds without media or drain progress",
    )
    parser.add_argument("--preflight-timeout", type=float, default=180)
    parser.add_argument("--max-radio-age", type=float, default=1.0)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--retry-delay", type=float, default=5)
    parser.add_argument("--recovery-timeout", type=float, default=120)
    default_hook = str(Path(__file__).resolve().parents[1] / "scripts/campaign_recover.sh")
    parser.add_argument(
        "--recovery-hook", action="append", default=[default_hook],
        help="executable called with REASON SCENARIO_ID ATTEMPT_ID after a failed attempt",
    )
    args = parser.parse_args()
    root = Path(args.campaign_dir)
    args.output = str(root)
    # Only one process may mutate the checkpoint or arm UEs at a time.
    runner_lock = (root / "runner.lock").open("w")
    try:
        fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError("another campaign runner is active") from error
    lock = json.loads((root / "manifest.lock.json").read_text())
    scenarios_path = root / "scenarios.jsonl"
    if sha256(scenarios_path) != lock["scenarios_sha256"]:
        raise RuntimeError("scenarios.jsonl does not match manifest.lock.json")
    scenarios = [json.loads(line) for line in scenarios_path.open() if line.strip()]
    if len(scenarios) != lock["scenario_count"]:
        raise RuntimeError("scenario count does not match manifest lock")
    if args.scenario_id:
        scenarios = [x for x in scenarios if x["scenario_id"] == args.scenario_id]
        if not scenarios:
            raise RuntimeError("scenario ID is not present in the immutable manifest")
    if args.limit is not None:
        if args.limit < 1:
            raise RuntimeError("--limit must be positive")
        scenarios = scenarios[: args.limit]
    state = State(root / "state.sqlite3", lock["scenarios_sha256"])
    Runner(args, scenarios, state).run()


if __name__ == "__main__":
    main()
