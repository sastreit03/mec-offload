import pytest
from muApp_mec.demand import Ledger
from muApp_mec.heuristic import weights


def test_AC01_predicted_actual_preserves_service():
    l = Ledger()
    l.descriptor(0, 1, 100, 1000, False)
    l.estimated_service(100, 0.8)
    l.descriptor(0, 1, 100, 1200, True)
    assert l.frames[0].remaining == 1120
    l.progress(0, 80, 1)
    assert l.frames[0].remaining == 1120


def test_AC02_duplicate_progress_corrections():
    l = Ledger()
    l.descriptor(0, 1, 100, 1000, True)
    l.progress(0, 200, 2)
    l.progress(0, 200, 2)
    l.progress(0, 100, 1)
    assert l.frames[0].remaining == 800
    with pytest.raises(ValueError):
        l.descriptor(0, 1, 100, 100, True, revision=1)
    assert l.frames[0].total == 1000
    with pytest.raises(ValueError):
        l.progress(0, 300, 3, True)
    assert l.frames[0].confirmed == 200


def test_AC03_overlapping_oldest_and_summary():
    l = Ledger()
    for fid, start, deadline, size in [
        (0, 1, 100, 100),
        (1, 2, 50, 200),
        (2, 3, 150, 300),
    ]:
        l.descriptor(fid, start, deadline, size, True)
    s = l.snapshot(1)
    assert (
        s["count"] == 3
        and s["remaining_bytes"] == 600
        and s["oldest"]["frame_id"] == 0
        and s["earliest_deadline_ns"] == 50
    )
    l.progress(0, 100, 1, True)
    assert l.snapshot(1)["oldest"]["frame_id"] == 1
    l.descriptor(0, 1, 100, 100, True)
    assert 0 not in l.frames


def test_AC04_expiry_is_not_cancellation():
    l = Ledger()
    l.descriptor(0, 1, 100, 100, True)
    s = l.snapshot(200)
    assert s["overdue_bytes"] == 100 and s["count"] == 1
    w, _ = weights({"ue": s}, {"ue": 10}, 200)
    assert w == {"ue": 1}
    l.estimated_service(100, 1)
    assert l.snapshot(200)["count"] == 1
    assert weights({"ue": l.snapshot(200)}, {"ue": 10}, 200)[0] == {}
    l.progress(0, 100, 1, True)
    assert not l.frames


def test_capacity_failure_does_not_clear_demand():
    l = Ledger(1)
    l.descriptor(0, 1, None, 100, True)
    with pytest.raises(ValueError):
        l.descriptor(1, 2, None, 100, True)
    assert not l.valid and l.frames[0].total == 100


@pytest.mark.parametrize(
    "change", ["symmetry", "radio", "progress", "deadline", "expired", "invalid"]
)
def test_HC01_to_HC04(change):
    def snapshot(size=100, received=0, deadline=2_000_000_000):
        l = Ledger()
        l.descriptor(0, 0, deadline, size, True)
        l.progress(0, received, 0)
        return l.snapshot(0)

    s = {"a": snapshot(), "b": snapshot()}
    caps = {"a": 10, "b": 10}
    if change == "radio":
        caps["a"] = 20
    if change == "progress":
        s["a"] = snapshot(received=50)
    if change == "deadline":
        s["a"] = snapshot(deadline=1_000_000_000)
    if change == "expired":
        s["a"] = snapshot(deadline=1)
    if change == "invalid":
        s["a"]["valid"] = False
    w, _ = weights(s, caps, 2)
    if change == "symmetry":
        assert w == {"a": 0.5, "b": 0.5}
    elif change == "invalid":
        assert "a" not in w
    else:
        assert w["a"] > w["b"]
    if w:
        assert sum(w.values()) == pytest.approx(1)


def test_retirement_stays_compact_with_one_permanently_lost_frame():
    l = Ledger()
    l.descriptor(0, 0, 1, 100, True)
    for fid in range(1, 10001):
        l.descriptor(fid, fid, 100000, 100, True)
        l.progress(fid, 100, 1, True)
    assert len(l.frames) == 1 and len(l.closed) == 1
    l.progress(0, 100, 1, True)
    assert l.watermark == 10000 and not l.closed
    l.descriptor(9999, 9999, 100000, 100, True)
    assert not l.frames
