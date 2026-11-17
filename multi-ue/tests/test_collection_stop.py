import asyncio
import time
from types import SimpleNamespace

import pytest

from mec_server.control import SendReceipt
from mec_server.engine import Video
from test_collection_abort import armed, CONTEXT


def video_for(engine, ue):
    v = Video("video", ue.uid, "detection", 1, 0, state="ACTIVE", current={"fps": 10})
    engine.videos[v.vid] = v
    engine.scenario_videos[ue.alias] = v.vid
    return v


def end_message(**extra):
    return dict(video_id="video", final_frame_id=69, frames_fully_sent=70,
                end_reason="scenario_stop", client_seq=10, **extra)


def test_seven_second_deadline_queues_stop_and_is_cancelled_on_abort(armed):
    async def exercise():
        engine, ue, sent = armed
        video_for(engine, ue)
        ue.scenario_ack = (CONTEXT["attempt_id"], ue.generation, ue.alias)
        def sender(m):
            sent.append(m)
            if m["type"] == "scenario_start":
                receipt = SendReceipt()
                receipt.finish()
                return receipt
        ue.sender = sender
        result = await engine.start_scenario(CONTEXT)
        handle = engine.scenario_stop_handle
        first_frame_timer = engine.scenario_first_frame_handle
        assert abs(first_frame_timer.when() - result["start_monotonic_ns"] / 1e9 - 1) < .01
        assert abs(handle.when() - result["start_monotonic_ns"] / 1e9 - 7) < .01
        assert not any(m["type"] == "scenario_stop" for m in sent)
        # Execute the captured callback without seven seconds of wall-clock sleep.
        handle.cancel()
        engine.stop_scenario(CONTEXT)
        assert sum(m["type"] == "scenario_stop" for m in sent) == 1
        engine.stop_scenario(CONTEXT)
        assert sum(m["type"] == "scenario_stop" for m in sent) == 1
        timeout = engine.scenario_stop_handle
        engine.invalidate_scenario("test_abort")
        assert timeout.cancelled()
        assert first_frame_timer.cancelled()
    asyncio.run(exercise())


@pytest.mark.parametrize("received,invalid", [(70, False), (61, False), (60, True), (80, True)])
def test_compare_after_late_arrivals_and_deduplicate_completions(armed, received, invalid):
    engine, ue, _ = armed
    v = video_for(engine, ue)
    engine.scenario_stop_sent = True
    engine.end_video(ue, end_message())
    assert not v.input_closed and not v.frame_counts_validated
    deadline = v.input_close_at
    engine.end_video(ue, end_message())
    assert v.input_close_at == deadline  # Replays cannot prolong arrival time.
    for fid in range(received):
        d = SimpleNamespace(video_id=v.vid, frame_id=fid)
        engine.progress_event(d, 100, True, time.monotonic_ns())
        engine.progress_event(d, 100, True, time.monotonic_ns())
    engine.progress_event(SimpleNamespace(video_id=v.vid, frame_id=1000), 50, False, 0)
    assert v.frames_fully_received == received
    engine.close_input(v, "video_end")
    assert bool(engine.scenario_error) is invalid
    assert v.frame_counts_validated is not invalid
    engine.progress_event(SimpleNamespace(video_id=v.vid, frame_id=1001), 100, True, 0)
    assert v.frames_fully_received == received  # Closed receive window is immutable.


def test_missing_end_and_early_source_end_cannot_succeed(armed):
    engine, ue, _ = armed
    video_for(engine, ue)
    engine.check_scenario_ends(dict(CONTEXT, attempt_id="old"))
    assert engine.scenario_error is None
    engine.check_scenario_ends(CONTEXT)
    assert engine.scenario_error["reason"] == "scenario_end_report_timeout"


def test_source_exhaustion_report_is_rejected_even_if_delayed_until_stop(armed):
    engine, ue, _ = armed
    video_for(engine, ue)
    engine.scenario_stop_sent = True
    m = end_message()
    m["end_reason"] = "source_ended"
    engine.end_video(ue, m)
    assert engine.scenario_error["reason"] == "scenario_ended_before_stop"


def test_zero_frame_end_is_reportable(armed):
    engine, ue, _ = armed
    v = video_for(engine, ue)
    engine.scenario_stop_sent = True
    m = end_message()
    m.update(final_frame_id=-1, frames_fully_sent=0)
    engine.end_video(ue, m)
    assert v.final_id == -1 and v.frames_fully_sent == 0


@pytest.mark.parametrize("arrival", [None, "partial", "on_time", "at_deadline", "late"])
def test_first_second_requires_complete_frame_from_every_ue(armed, arrival):
    engine, ue, sent = armed
    first = video_for(engine, ue)
    other_messages = []
    other, _ = engine.hello(None, "12.1.1.3", other_messages.append)
    other.alias = "linux1"
    engine.scenario_assignments[other.alias] = {}
    second = Video("other", other.uid, "detection", 2, 0, state="ACTIVE", current={"fps": 10})
    engine.videos[second.vid] = second
    engine.scenario_videos[other.alias] = second.vid
    deadline = 2_000_000_000
    engine.progress_event(SimpleNamespace(video_id=first.vid, frame_id=0), 100, True, deadline - 1)
    if arrival is not None:
        stamp = deadline - 1 if arrival in ("partial", "on_time") else deadline
        if arrival == "late": stamp += 1
        engine.progress_event(SimpleNamespace(video_id=second.vid, frame_id=0),
                              100, arrival != "partial", stamp)
    # Calling after reception also tests that late frames cannot rescue the attempt.
    engine.check_scenario_first_frames(CONTEXT, deadline)
    if arrival == "on_time":
        assert engine.scenario_error is None
        assert not first.input_closed and not second.input_closed
    else:
        assert engine.scenario_error["reason"] == "scenario_first_frame_timeout"
        assert engine.scenario_error["missing_ues"] == ["linux1"]
        assert first.input_closed and second.input_closed
        assert any(m["type"] == "scenario_abort" for m in sent)
        assert any(m["type"] == "scenario_abort" for m in other_messages)


def test_old_first_frame_callback_cannot_abort_new_attempt(armed):
    engine, ue, _ = armed
    video_for(engine, ue)
    engine.check_scenario_first_frames(dict(CONTEXT, attempt_id="old"), 1)
    assert engine.scenario_error is None
    engine.invalidate_scenario("earlier_failure")
    engine.check_scenario_first_frames(CONTEXT, 1)
    assert engine.scenario_error["reason"] == "earlier_failure"


def test_packet_endpoints_preserve_receiver_clock_mapping(armed):
    engine, ue, _ = armed
    v = video_for(engine, ue)
    v.radio_frames[0] = dict(progress_seq=0, unique_bytes=0, complete=False)
    f = SimpleNamespace(times={}, complete_ns=None)
    engine.frames[(v.vid, 0)] = f
    d = SimpleNamespace(video_id=v.vid, frame_id=0)
    ue.network = None
    # Values supplied by the receive callback must survive queue delays unchanged.
    engine.progress_event(d, 40, False, 100, 1000)
    engine.progress_event(d, 40, False, 110, 1010)  # Duplicate reception counts.
    assert f.times == dict(last_packet_reception_monotonic_ns=110,
                          last_packet_reception_unix_ns=1010)
    assert engine.progress[(v.vid, 0)]['measurement_unix_ns'] == 1010
    engine.progress_event(d, 100, True, 120, 1020)
    assert f.complete_ns == 120
    assert f.times['complete_reception_monotonic_ns'] == 120
    assert f.times['complete_reception_unix_ns'] == 1020
    assert f.times['last_packet_reception_unix_ns'] == 1020
