import asyncio
from unittest.mock import Mock

import pytest
from muApp_mec.demand import Ledger
from muApp_mec.heuristic import weights
from muApp_mec.runtime import Runtime


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


def test_authoritative_sync_retires_stale_frames_and_sessions():
    runtime = Runtime.__new__(Runtime)
    runtime.cfg = {"association": {}, "max_sessions": 64}
    runtime.source = "source"
    runtime.run_id = None
    runtime.sessions = {}
    runtime.retired_sessions = {}
    runtime.sync_revision = -1
    runtime.event = Mock()

    session = dict(
        ue_id="ue",
        video_id="video",
        media_epoch=1,
        outstanding=[
            dict(
                frame_id=4,
                tx_unix_ns=100,
                network_deadline_unix_ns=200,
                encoded_bytes=1000,
                config_version=1,
                unique_bytes=100,
                progress_seq=1,
                complete=False,
            )
        ],
    )

    async def exercise():
        await runtime.exchange(
            dict(
                schema_version=1,
                type="sync",
                run_id="run",
                awareness_revision=1,
                associations=[],
                sessions=[session],
            )
        )
        assert 4 in runtime.sessions["video"]["ledger"].frames

        # Absence from a newer full snapshot is authoritative completion evidence.
        await runtime.exchange(
            dict(
                schema_version=1,
                type="sync",
                run_id="run",
                awareness_revision=3,
                associations=[],
                sessions=[dict(session, outstanding=[])],
            )
        )
        assert 4 not in runtime.sessions["video"]["ledger"].frames

        # A queued update created before the snapshot cannot reopen retired demand.
        result = await runtime.exchange(
            dict(
                schema_version=1,
                type="descriptor",
                run_id="run",
                awareness_revision=2,
                ue_id="ue",
                video_id="video",
                media_epoch=1,
                frame_id=4,
                tx_unix_ns=100,
                network_deadline_unix_ns=200,
                encoded_bytes=1000,
                config_version=1,
            )
        )
        assert result["status"] == "stale"
        assert 4 not in runtime.sessions["video"]["ledger"].frames

        await runtime.exchange(
            dict(
                schema_version=1,
                type="sync",
                run_id="run",
                awareness_revision=4,
                associations=[],
                sessions=[],
            )
        )
        assert "video" not in runtime.sessions

    asyncio.run(exercise())


def test_closed_sessions_release_capacity_and_reject_delayed_updates():
    runtime = Runtime.__new__(Runtime)
    runtime.cfg = {"association": {}, "max_sessions": 2}
    runtime.source = "source"
    runtime.run_id = "run"
    runtime.sessions = {}
    runtime.retired_sessions = {}
    runtime.sync_revision = 0
    runtime.event = Mock()

    async def exercise():
        revision = 0
        for index in range(5):
            vid = f"video-{index}"
            revision += 1
            await runtime.exchange(
                dict(
                    schema_version=1,
                    type="session",
                    run_id="run",
                    awareness_revision=revision,
                    ue_id="ue",
                    video_id=vid,
                    media_epoch=1,
                    input_closed=False,
                )
            )
            revision += 1
            await runtime.exchange(
                dict(
                    schema_version=1,
                    type="session",
                    run_id="run",
                    awareness_revision=revision,
                    ue_id="ue",
                    video_id=vid,
                    media_epoch=1,
                    input_closed=True,
                )
            )
            assert not runtime.sessions

        # A delayed plan cannot recreate a closed identity.
        await runtime.exchange(
            dict(
                schema_version=1,
                type="session",
                run_id="run",
                awareness_revision=revision + 1,
                ue_id="ue",
                video_id="video-4",
                media_epoch=1,
                input_closed=False,
            )
        )
        assert "video-4" not in runtime.sessions

        # A full snapshot supersedes and releases the compact fences.
        await runtime.exchange(
            dict(
                schema_version=1,
                type="sync",
                run_id="run",
                awareness_revision=revision + 2,
                associations=[],
                sessions=[],
            )
        )
        assert not runtime.retired_sessions

    asyncio.run(exercise())


def test_closed_session_retires_after_last_reception_completes():
    runtime = Runtime.__new__(Runtime)
    runtime.cfg = {"association": {}, "max_sessions": 2}
    runtime.source = "source"
    runtime.run_id = "run"
    runtime.sessions = {}
    runtime.retired_sessions = {}
    runtime.sync_revision = 0
    runtime.event = Mock()

    async def exercise():
        base = dict(
            schema_version=1,
            run_id="run",
            ue_id="ue",
            video_id="video",
            media_epoch=1,
        )
        await runtime.exchange(
            dict(base, type="session", awareness_revision=1, input_closed=False)
        )
        await runtime.exchange(
            dict(
                base,
                type="descriptor",
                awareness_revision=2,
                frame_id=7,
                tx_unix_ns=100,
                network_deadline_unix_ns=200,
                encoded_bytes=1000,
                config_version=1,
            )
        )
        await runtime.exchange(
            dict(base, type="session", awareness_revision=3, input_closed=True)
        )
        assert "video" in runtime.sessions

        await runtime.exchange(
            dict(
                base,
                type="progress",
                awareness_revision=4,
                frame_id=7,
                unique_bytes=1000,
                progress_seq=1,
                complete=True,
            )
        )
        assert "video" not in runtime.sessions
        assert runtime.retired_sessions["video"][:2] == ("ue", 1)

    asyncio.run(exercise())


def test_pending_expectations_predict_and_reconcile_at_activation():
    runtime = Runtime.__new__(Runtime)
    runtime.cfg = {"max_sessions": 4}
    runtime.sessions = {}
    runtime.retired_sessions = {}
    runtime.sync_revision = 0
    runtime.event = Mock()

    initial = dict(
        applied=False,
        control_version=1,
        config_version=1,
        plan_version=1,
        first_tx_unix_ns=1_000_000_000,
        first_frame_id=0,
        fps=10,
        gop_frames=10,
        network_budget_ns=100_000_000,
        size_profile={"I": 1000, "P": 100},
    )
    session = dict(
        ue_id="ue", video_id="video", media_epoch=1, expectation=initial
    )
    runtime._session(session)
    runtime.predict(1_500_000_000)
    ledger = runtime.sessions["video"]["ledger"]
    assert set(ledger.frames) == set(range(6))
    assert all(not frame.actual for frame in ledger.frames.values())

    runtime._apply_expectation(
        runtime.sessions["video"], dict(initial, applied=True), 0, 1_000_000_000
    )
    pending = dict(
        initial,
        control_version=2,
        config_version=2,
        plan_version=2,
        first_tx_unix_ns=2_000_000_000,
        first_frame_id=10,
    )
    runtime._session(dict(session, expectation=pending))
    runtime.predict(2_100_000_000)

    assert all(ledger.frames[fid].config == 1 for fid in range(10))
    assert ledger.frames[10].config == ledger.frames[11].config == 2

    # Delayed pre-boundary evidence does not cancel the advertised transition.
    runtime._apply_expectation(
        runtime.sessions["video"], dict(initial, applied=True), 5, 1_500_000_000
    )
    assert runtime.sessions["video"]["pending_expectation"]["plan_version"] == 2

    # Ordered old-plan evidence at the boundary means the transition was missed.
    runtime._apply_expectation(
        runtime.sessions["video"], dict(initial, applied=True), 10, 2_000_000_000
    )
    runtime.predict(2_100_000_000)
    assert "pending_expectation" not in runtime.sessions["video"]
    assert ledger.frames[10].config == ledger.frames[11].config == 1
