import uuid
import pytest
from mec_contracts.rtp import (
    Descriptor,
    packetize,
    parse_packet,
    Assembly,
    canonical_au,
)
from mec_contracts.wire import policy_features, loads
from muApp_mec.legacy_wire import metrics_pb2, control_weights_pb2


def descriptor(nals):
    return Descriptor(
        str(uuid.uuid4()), 1, 0, 0, 1, 1, 1, len(canonical_au(nals)), 1, 2, 3, 4, 15
    )


def test_descriptor_packet_fragmentation_reorder_and_duplicates():
    nals = [b"\x67abc", b"\x68def", b"\x65" + b"x" * 5000]
    d = descriptor(nals)
    assert len(d.pack()) == 96 and Descriptor.unpack(d.pack()) == d
    packets = list(packetize(nals, d, 65534, 123, 456, 1200))
    assert max(map(len, packets)) <= 1200
    a = Assembly(d, 10000)
    for p in reversed(packets):
        a.add(parse_packet(p))
        a.add(parse_packet(p))
    assert a.unique_bytes == d.encoded_bytes and a.complete


def test_packet_loss_never_confirms_complete():
    nals = [b"\x65" + b"x" * 4000]
    d = descriptor(nals)
    ps = list(packetize(nals, d, 1, 2, 3, 1200))
    a = Assembly(d, 5000)
    for p in ps[:1] + ps[2:]:
        a.add(parse_packet(p))
    assert not a.complete


def test_bad_descriptor_and_nonfinite_json():
    d = descriptor([b"\x65ab"])
    a = bytearray(d.pack())
    a[2] = 1
    with pytest.raises(ValueError):
        Descriptor.unpack(a)
    with pytest.raises(ValueError):
        loads('{"x":NaN}')


@pytest.mark.parametrize("frame_type", [0, 2, 4])
def test_only_idr_and_p_descriptor_types_are_allowed(frame_type):
    with pytest.raises(ValueError, match="IDR"):
        Descriptor(
            str(uuid.uuid4()), 1, 0, 0, 1, 1, frame_type, 100, 1, 2, 3, 4, 15
        )


def test_policy_features_recursively_exclude_transport():
    assert policy_features(
        {
            "ue_id": "u",
            "history": [{"rnti": 1, "ue_ip": "x", "ue_id": "u", "raw_token": "t"}],
        }
    ) == {"ue_id": "u", "history": [{"ue_id": "u"}]}


def test_OAI_wire_roundtrip():
    m = metrics_pb2.Metrics(tti_cnt=123)
    m.ue_metrics.add(rnti=400, rx_bytes=1.25)
    assert (
        metrics_pb2.Metrics.FromString(m.SerializeToString()).ue_metrics[0].rx_bytes
        == 1.25
    )
    w = control_weights_pb2.SchedulingWeights(
        ran_index=m.tti_cnt, ul_weights=[400, 0.5]
    )
    assert not w.weights
    assert (
        control_weights_pb2.SchedulingWeights.FromString(
            w.SerializeToString()
        ).ran_index
        == 123
    )
