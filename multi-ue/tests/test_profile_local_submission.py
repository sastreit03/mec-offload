import hashlib
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace

from mec_contracts.rtp import Descriptor, canonical_au, packetize
from profile_local_submission import LocalReceiver, attach_receipts, measure_stream, summarize


def test_real_loopback_fragmentation_wrap_and_missing_packet():
    video = str(uuid.uuid4())
    receiver = LocalReceiver(video, 1024*1024)
    try:
        nals = [b'\x65' + b'x'*3000]
        descriptor = Descriptor(video, 1, 0, 0, 1, 1, 1, len(canonical_au(nals)))
        packets = list(packetize(nals, descriptor, 65535, 1, 2, 1200))
        missing_descriptor = Descriptor(video, 1, 1, 0, 1, 1, 1, len(canonical_au(nals)))
        missing = list(packetize(nals, missing_descriptor, 2, 2, 2, 1200))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('127.0.0.1', receiver.settings['port']))
            for packet in packets + missing[1:]:
                sock.send(packet)
        receipt = receiver.finish(len(packets)+len(missing), .1)
        assert receipt['frames'][0]['complete']
        assert not receipt['frames'][1]['complete']
        rows = []
        for fid, datagrams in enumerate([packets, missing]):
            rows.append(dict(frame_id=fid, submitted_packets=len(datagrams),
                             submitted_udp_bytes=sum(map(len, datagrams)),
                             submitted_sha256=hashlib.sha256(b''.join(datagrams)).hexdigest()))
        attach_receipts(rows, receipt, 0)
        assert rows[0]['receipt_verified']
        assert not rows[1]['receipt_verified']
        # Matching counts alone must not permit a corrupted payload to pass.
        rows[0]['submitted_sha256'] = 'wrong'
        attach_receipts(rows, receipt, 0)
        assert not rows[0]['receipt_verified']
    finally:
        receiver.close()
    assert not receiver.process.is_alive()


def test_full_sender_with_synthetic_encoder():
    state = SimpleNamespace(reads=0, closed=False, released=False)

    class Capture:
        def isOpened(self):
            return True

        def read(self):
            state.reads += 1
            return state.reads <= 4, object()

        def release(self):
            state.released = True

    class Encoder:
        def __init__(self, *args):
            pass

        def frame(self, bgr, fid, fps, keyframe, bitrate):
            return [(b'\x65' if keyframe else b'\x41') + b'x'*4000]

        def close(self):
            state.closed = True

    args = SimpleNamespace(width=1280, height=720, fps=30, gop_frames=2,
                           receive_buffer_bytes=1024*1024, mtu=1200, drain_timeout=1)
    cv2 = SimpleNamespace(VideoCapture=lambda _: Capture(), resize=lambda bgr, size: bgr)
    rows, info = measure_stream(Path('test.mov'), 500000, args, cv2, Encoder)
    summary = summarize(rows, info, 30)
    assert len(rows) == 4
    assert summary['all_receipts_verified']
    assert summary['submitted_packets'] == summary['received_packets'] == 16
    assert [r['frame_type'] for r in rows] == ['I', 'P', 'I', 'P']
    for row in rows:
        assert row['send_end_ns'] >= row['send_start_ns'] >= row['packetize_end_ns']
        assert row['deadline_missed'] == (row['send_end_ns'] > row['deadline_ns'])
        assert row['processing_ns'] == row['send_end_ns'] - row['read_start_ns']
    assert state.closed and state.released
