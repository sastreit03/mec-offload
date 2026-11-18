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
