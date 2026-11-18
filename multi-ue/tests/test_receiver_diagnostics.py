from types import SimpleNamespace

from mec_server.media import Receiver


def test_packet_summaries_distinguish_jitter_drops_and_duplicates():
    events = []
    receiver = Receiver("video", 1, 5000, SimpleNamespace(mode="collection"),
                        lambda kind, value: events.append((kind, value)))
    descriptor = SimpleNamespace(frame_id=2)
    for stage, sequences in [("udp_input", [4, 5, 5, 6]), ("jitter_output", [4, 6])]:
        for seq in sequences:
            packet = b"\x80\x60" + seq.to_bytes(2, "big") + (9000).to_bytes(4, "big")
            receiver._observe_packet(stage, packet, descriptor, 100000)
    receiver.close()
    summaries = {value["stage"]: value for kind, value in events}
    assert summaries["udp_input"]["packets"] == 4
    assert summaries["udp_input"]["sequences"] == [4, 5, 6]
    assert summaries["jitter_output"]["sequences"] == [4, 6]
    assert summaries["jitter_output"]["rtp_timestamp"] == 9000
    receiver.close()
    assert len(events) == 2


def test_packet_diagnostics_disabled_outside_collection():
    events = []
    receiver = Receiver("video", 1, 5000, SimpleNamespace(mode="experimental"),
                        lambda *event: events.append(event))
    receiver._observe_packet("udp_input", b"", SimpleNamespace(frame_id=0), 0)
    receiver.close()
    assert not events


def receiver_with_pending():
    events = []
    receiver = Receiver("video", 1, 5000,
                        SimpleNamespace(mode="collection", receive_loss_s=1, decode_wait_s=2),
                        lambda kind, value: events.append((kind, value)))
    for fid in (43, 44, 49, 50, 51):
        receiver.decode_pending[fid] = (SimpleNamespace(
            frame_id=fid, frame_type=1 if fid == 50 else 3), None)
    return receiver, events


def test_drop_resolves_only_mapped_frame_and_ignores_forwarded_duplicate():
    receiver, events = receiver_with_pending()
    receiver.sequence_frames[100] = 43
    receiver.sequence_frames[101] = 44
    receiver.packet_evidence[("jitter_output", 44)] = {"sequences": {101}}
    receiver._jitter_drop(101, "too-late", "duplicate")
    receiver._jitter_drop(999, "drop-on-latency", "unknown")
    assert 44 in receiver.decode_pending
    receiver._jitter_drop(100, "drop-on-latency", "known")
    receiver._jitter_drop(100, "too-late", "repeat")
    failures = [v for k, v in events if k == "frame_failure"]
    assert [(v["descriptor"].frame_id, v["reason"]) for v in failures] == [(43, "jitterbuffer_drop")]
    assert set(receiver.decode_pending) == {44, 49, 50, 51}


def test_keyframe_recovery_closes_only_earlier_decode_gaps():
    receiver, events = receiver_with_pending()
    receiver._decoded_frame(receiver.decode_pending[44][0])
    assert 43 in receiver.decode_pending  # An ordinary decoded P frame is not IDR recovery.
    receiver._decoded_frame(receiver.decode_pending[50][0])
    assert set(receiver.decode_pending) == {51}
    assert [(v["descriptor"].frame_id, v["reason"]) for k, v in events
            if k == "frame_failure"] == [(43, "decode_gap_before_keyframe"),
                                          (49, "decode_gap_before_keyframe")]


def test_decode_timeout_starts_at_complete_reception_not_first_packet(monkeypatch):
    receiver, events = receiver_with_pending()
    monkeypatch.setattr("mec_server.media.time.monotonic_ns", lambda: 10_000_000_000)
    d, _ = receiver.decode_pending[43]
    receiver.decode_pending[43] = (d, 8_000_000_001)
    receiver.tick()
    assert not events  # Almost two seconds is still within the limit.
    receiver.decode_pending[43] = (d, 8_000_000_000)
    receiver.tick()
    assert 43 not in receiver.decode_pending
    assert 44 in receiver.decode_pending  # Not completely received: transport timeout owns it.
    assert [v["reason"] for k, v in events if k == "frame_failure"] == ["decode_wait_timeout"]


def test_decoded_frame_is_not_timed_out_during_inference(monkeypatch):
    receiver, events = receiver_with_pending()
    d, _ = receiver.decode_pending[43]
    receiver.decode_pending[43] = (d, 1)
    receiver._decoded_frame(d)
    monkeypatch.setattr("mec_server.media.time.monotonic_ns", lambda: 10_000_000_000)
    receiver.tick()
    assert not events


def test_tracker_advances_after_receiver_failures_are_resolved():
    from mec_server.engine import Engine

    receiver, events = receiver_with_pending()
    receiver._decoded_frame(receiver.decode_pending[50][0])
    frames = {("video", fid): SimpleNamespace(vid="video", fid=fid, state="RECEIVING")
              for fid in (43, 44, 49, 50)}
    v = SimpleNamespace(vid="video", tracker_busy=False, tracking_ready={50: frames[("video", 50)]})
    scheduled = []
    engine = SimpleNamespace(collection_mode=True, frames=frames,
                             task=scheduled.append, track=lambda video, frame: frame.fid)
    Engine.tracker_tick(engine, v, 0)
    assert scheduled == []
    for kind, value in events:
        if kind == "frame_failure":
            frames.pop(("video", value["descriptor"].frame_id))
    Engine.tracker_tick(engine, v, 0)
    assert scheduled == [50]
    assert v.tracker_busy
