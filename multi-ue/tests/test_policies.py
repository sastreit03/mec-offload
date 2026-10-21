import pytest
from mec_server.policies import HeuristicPriority, HeuristicGoP, validate_joint
from mec_server.statistics import History, NetworkFilter


def test_priority_thresholds():
    p = HeuristicPriority()
    assert p.propose(1, 1, 0, 0, 0) is None
    assert p.propose(1, 0.9, 0, 0, 0) == -5
    assert p.propose(1, 0.6, 0, 0, 0) == -3
    assert p.propose(1, 0.1, 0, 0, 0) == 0


def test_histories_need_ten_completed_values():
    h = History(5, "mean")
    for i in range(9):
        h.add(1)
    assert h.value == 5
    h.add(1, False)
    assert h.value == 5
    h.add(1)
    assert h.value == 1


def test_filter_dedup_and_gap_reset():
    p = dict(mean_s=0.1, P0_s2=0.1, R_s2=0.1, q_s2_per_s=0.001, V_frame_s2=0.01)
    f = NetworkFilter(p)
    assert f.observe(("v", 1), 100, 1000, 1e6, 1)
    mu = f.mu
    assert not f.observe(("v", 1), 10000, 1000, 1e6, 2)
    assert f.mu == mu
    assert not f.observe(("v", 2), 100, 0, 1e6, 7, False)
    assert f.mu == p["mean_s"]


def test_joint_validator_and_search_failure():
    cs = {
        "a": dict(bitrate_bps=100, processing_s=0.01),
        "b": dict(bitrate_bps=50, processing_s=0.02),
    }
    u = {
        "u": dict(
            candidate_ids=["a", "b"],
            slo_s=0.15,
            mu_s=0.01,
            variance_s2=0.001,
            radio_bps_per_unit=10,
            fps=30,
        )
    }
    p = HeuristicGoP()
    a = p.propose(u, cs, 6, 1)
    assert a == {"u": "b"} and validate_joint(a, u, cs, 6, 1)
    assert p.propose(u, cs, 1, 0.1) is None
    assert not validate_joint({"u": "bad"}, u, cs, 100, 100)


def test_stale_and_prolonged_outage_thresholds(monkeypatch):
    from types import SimpleNamespace
    from mec_server.engine import Engine

    now = 10_000_000_000
    monkeypatch.setattr("mec_server.engine.time.time_ns", lambda: now)
    e = Engine.__new__(Engine)
    e.cfg = SimpleNamespace(
        mode="experimental", ran_telemetry=dict(stale_ms=300, prolonged_outage_s=5)
    )
    e.ues = {"u": SimpleNamespace(connection_id="c")}
    e.last_usable = {}
    r = dict(association_valid=True, connection_id="c")
    e.last_report = dict(
        window_end_unix_ns=now, trace_events=1, trace_coverage_valid=True, ues={"u": r}
    )
    assert e.usable_radio("u")
    now += 300_000_001
    assert not e.usable_radio("u")
    assert e.usable_radio("u", admission=True)
    now += 4_700_000_000
    assert not e.usable_radio("u", admission=True)
    e.ues["u"].connection_id = "new"
    assert not e.usable_radio("u", admission=True)


def test_sending_control_does_not_reset_telemetry_grace():
    from types import SimpleNamespace
    from mec_server.engine import Engine

    e = Engine.__new__(Engine)
    out = []
    e.ues = {"u": SimpleNamespace(connected=True, sender=out.append)}
    e.last_usable = {"u": 123}
    e.send("u", {"type": "test"})
    assert e.last_usable["u"] == 123
