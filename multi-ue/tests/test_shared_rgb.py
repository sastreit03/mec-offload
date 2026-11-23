"""Shared RGB ownership, actual Gst maps, metadata IPC and optional CUDA checks."""
import asyncio
import os
import pickle
import queue
import time
from multiprocessing.shared_memory import SharedMemory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from mec_server.shared_rgb import RGBPool, RGBMapping
from mec_server.handoff import ReceiverHandoff
from mec_server.media import Receiver
from mec_server.preview import Preview
from mec_server import workers


def test_padded_source_generation_exhaustion_and_delayed_unlink():
    pool = RGBPool(1, 4096)
    padded = np.arange(4*32, dtype=np.uint8).reshape(4, 32)
    image = np.ndarray((4, 7, 3), np.uint8, buffer=padded, strides=(32, 3, 1))
    first = pool.copy(image)
    name = first.descriptor.region
    consumer = first.retain()
    assert len(pickle.dumps(first.wire())) < 512
    mapping = RGBMapping(first.wire())
    np.testing.assert_array_equal(mapping.image, image)
    assert not mapping.image.flags.writeable
    mapping.close()
    first.release()
    assert pool.copy(image) is None
    consumer.release()
    second = pool.copy(image)
    assert second.descriptor.generation == 2
    assert pool.stats()['copies'] == 2
    pool.close()
    assert pool.stats()['mapped']  # still leased
    np.testing.assert_array_equal(second.array(), image)
    second.release()
    second.release()  # duplicate local releases are harmless
    assert not pool.stats()['mapped']
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=name)
    with pytest.raises(RuntimeError):
        second.retain()


def test_invalid_descriptor_cannot_read_outside_slot():
    pool = RGBPool(1, 100)
    lease = pool.copy(np.zeros((2, 3, 3), np.uint8))
    try:
        wire = lease.wire()
        wire['row_stride'] = pool.slot_bytes
        with pytest.raises(ValueError, match='descriptor'):
            RGBMapping(wire)
    finally:
        lease.release()
        pool.close()


def test_stale_generation_cannot_be_mapped_or_release_new_owner():
    from threading import RLock
    pool = RGBPool(1, 100)
    first = pool.copy(np.zeros((2, 3, 3), np.uint8))
    old = first.wire()
    first.release()
    current = pool.copy(np.ones((2, 3, 3), np.uint8))
    w = workers.Workers.__new__(workers.Workers)
    w.lease_lock = RLock()
    w.input_leases = {('detection', ('v', 0)): current}
    try:
        with pytest.raises(ValueError, match='stale'):
            RGBMapping(old)
        w.release_input('detection', ['v', 0], old)
        assert pool.stats()['in_use'] == 1
        w.release_input('detection', ['v', 0], current.wire())
        assert pool.stats()['in_use'] == 0
    finally:
        current.release()
        pool.close()


def test_backing_allocation_failure_is_recoverable(monkeypatch):
    import errno
    def full(*args):
        raise OSError(errno.ENOSPC, 'shared memory full')
    monkeypatch.setattr('mec_server.shared_rgb.os.posix_fallocate', full)
    pool = RGBPool(1, 100)
    with pytest.raises(OSError):
        pool.copy(np.zeros((2, 3, 3), np.uint8))
    assert not pool.stats()['mapped'] and not pool.stats()['in_use']
    pool.close()


def test_submission_failure_does_not_keep_worker_lease():
    from threading import RLock
    w = workers.Workers.__new__(workers.Workers)
    w.available = lambda *a: True
    w.closing, w.input_leases, w.lease_lock = False, {}, RLock()
    def fail(job): raise ValueError('queue closed')
    w.inputs = {'detection': SimpleNamespace(put_nowait=fail)}
    pool = RGBPool(1, 100)
    lease = pool.copy(np.zeros((2, 3, 3), np.uint8))
    try:
        assert not w.submit('detection', 0, dict(job_id=['v', 0], rgb=lease))
        assert not w.input_leases and pool.stats()['references'] == 1
    finally:
        lease.release()
        pool.close()


def test_handoff_overflow_releases_only_rejected_lease():
    async def exercise():
        pool = RGBPool(2, 100)
        seen = []
        handoff = ReceiverHandoff(asyncio.get_running_loop(), lambda k, d: seen.append(d),
                                  lambda *args: None, pictures=1)
        a, b = [pool.copy(np.zeros((2, 3, 3), np.uint8)) for _ in range(2)]
        handoff('decoded', dict(rgb=a))
        handoff('decoded', dict(rgb=b))
        assert b.released and not a.released
        await asyncio.sleep(0)
        assert seen == [dict(rgb=a)]
        a.release()
        pool.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['resolved', 'incomplete', 'closed', 'quarantined', 'submit'])
def test_engine_rejection_releases_rgb(failure):
    from mec_server.engine import Engine, Frame, Video
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(mode='development')
    engine.frames, engine.videos = {}, {}
    engine.quarantined = set()
    engine.fail_frame = Mock()
    d = SimpleNamespace(video_id='v', frame_id=0, tx_unix_ns=1)
    config = dict(id='c', application='detection')
    f = Frame('v', 0, d, 1, config, complete_ns=2, deadline_ns=2**63-1)
    v = Video('v', 'u', 'detection', 1, 0, state='ACTIVE')
    engine.frames['v', 0] = f
    engine.videos['v'] = v
    if failure == 'resolved': v.resolved.add(0)
    if failure == 'incomplete': f.complete_ns = None
    if failure == 'closed': v.input_closed = True
    if failure == 'quarantined': engine.quarantined.add('c')
    if failure == 'submit':
        engine.priority = SimpleNamespace(propose=lambda *a: 0, version='test')
        engine.queue_history = SimpleNamespace(value=0)
        engine.processing = lambda *a: 0
        engine.record = Mock()
        engine.workers = SimpleNamespace(submit=lambda *a: False)
    pool = RGBPool(1, 100)
    lease = pool.copy(np.zeros((2, 3, 3), np.uint8))
    engine.decoded(d, lease, 3)
    assert pool.stats()['in_use'] == 0
    pool.close()


def test_preview_replacement_and_cancel_wait_for_encoder():
    async def exercise():
        entered, finish = Event(), Event()
        def encode(image):
            entered.set()
            assert finish.wait(5)
            assert np.all(image == 7)
            return b'jpeg'
        engine = SimpleNamespace(ues={'u': SimpleNamespace(alias='a', connected=True, active_video='v')})
        preview = Preview(engine, encode)
        channel = preview.watch('a')
        pool = RGBPool(2, 100)
        a = pool.copy(np.full((2, 3, 3), 1, np.uint8))
        b = pool.copy(np.full((2, 3, 3), 7, np.uint8))
        preview.offer('u', 'v', 0, a)
        a.release()
        preview.offer('u', 'v', 1, b)
        b.release()
        assert pool.stats()['in_use'] == 1
        try:
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            assert entered.is_set()
            closing = asyncio.create_task(preview.close())
            await asyncio.sleep(.02)
            assert not closing.done() and pool.stats()['in_use'] == 1
            finish.set()
            await closing
            assert pool.stats()['in_use'] == 0
        finally:
            finish.set()
            await preview.close()
            pool.close()
    asyncio.run(exercise())


def gst_receiver():
    gi = pytest.importorskip('gi')
    gi.require_version('Gst', '1.0')
    gi.require_version('GstVideo', '1.0')
    from gi.repository import Gst, GstVideo
    Gst.init(None)
    events = []
    cfg = SimpleNamespace(mode='development', decoded_max_bytes=4096, shared_rgb_slots=1)
    receiver = Receiver('v', 1, 5000, cfg, lambda k, d: events.append((k, d)))
    receiver.Gst, receiver.GstVideo = Gst, GstVideo
    return receiver, events, Gst, GstVideo


@pytest.mark.parametrize('video_meta', [False, True])
def test_real_gst_borrowed_map_stride_identity_and_exhaustion(video_meta):
    receiver, events, Gst, GstVideo = gst_receiver()
    caps = Gst.Caps.from_string('video/x-raw,format=RGB,width=5,height=3,framerate=30/1')
    info = GstVideo.VideoInfo.new_from_caps(caps)
    assert info.stride[0] > 5*3
    stride, offset = (32, 5) if video_meta else (info.stride[0], 0)
    size = offset+3*stride
    raw = np.arange(size, dtype=np.uint8)
    buffer = Gst.Buffer.new_allocate(None, size, None)
    if video_meta:
        GstVideo.buffer_add_video_meta_full(buffer, GstVideo.VideoFrameFlags.NONE,
            GstVideo.VideoFormat.RGB, 5, 3, 1, [offset, 0, 0, 0], [stride, 0, 0, 0])
    buffer.fill(0, raw.tobytes())
    buffer.pts = 123
    sample = Gst.Sample.new(buffer, caps, None, None)
    d = SimpleNamespace(frame_id=1)
    receiver.pts[123] = d
    with receiver._mapped(buffer) as borrowed:
        assert isinstance(borrowed, memoryview)
    receiver._sample(SimpleNamespace(emit=lambda _: sample))
    kind, event = events.pop()
    assert kind == 'decoded' and event['descriptor'] is d
    lease = event['rgb']
    expected = np.ndarray((3, 5, 3), np.uint8, buffer=raw, offset=offset, strides=(stride, 3, 1))
    np.testing.assert_array_equal(lease.array(), expected)
    receiver.pts[123] = SimpleNamespace(frame_id=2)
    receiver._sample(SimpleNamespace(emit=lambda _: sample))
    assert events[-1][1]['reason'] == 'shared_rgb_pool_exhausted'
    receiver.close()
    assert receiver.rgb_pool.stats()['mapped']
    np.testing.assert_array_equal(lease.array(), expected)
    lease.release()
    assert not receiver.rgb_pool.stats()['mapped']


def test_mapped_rtp_retains_payload_and_validates_conflicting_duplicate():
    import uuid
    from mec_contracts.rtp import Descriptor, Assembly, packetize, parse_packet
    nals = [b'\x65'+b'x'*400]
    d = Descriptor(str(uuid.uuid4()), 1, 0, 0, 1, 1, 1, 405)
    raw = bytearray(next(packetize(nals, d, 10, 20, 30, mtu=256)))
    a = Assembly(d)
    a.add(parse_packet(memoryview(raw)))
    retained = a.packets[10]
    assert isinstance(retained, bytes)
    a.add(parse_packet(memoryview(raw)))
    assert a.packets[10] is retained
    raw[-1] ^= 1
    with pytest.raises(ValueError, match='conflicting RTP sequence'):
        a.add(parse_packet(memoryview(raw)))
    assert a.packets[10] == retained


def start_mock_workers():
    specs = [dict(application='detection', model=str(n)) for n in range(4)]
    w = workers.Workers(dict(configurations=specs), dict(backend='mock',
                        applications=['detection'], priority_streams=[0]))
    w.start()
    deadline = time.monotonic()+20
    while not w.ready:
        list(w.poll())
        assert time.monotonic() < deadline
        time.sleep(.005)
    return w


def test_spawn_metadata_only_and_consumption_reuses_slots():
    w = start_mock_workers()
    pool = RGBPool(2, 4096)
    completed = []
    try:
        for fid in range(12):
            lease = pool.copy(np.full((5, 7, 3), fid, np.uint8))
            assert lease is not None
            assert w.submit('detection', 0, dict(job_id=['v', fid], priority=0,
                            configuration={}, rgb=lease))
            lease.release()
            deadline = time.monotonic()+10
            while len(completed) <= fid or w.input_leases:
                completed.extend(m for m in w.poll() if m['type'] == 'completed')
                assert time.monotonic() < deadline
                time.sleep(.001)
        assert [m['job_id'][1] for m in completed] == list(range(12))
        assert all(m['payload']['height'] == 5 and m['payload']['width'] == 7 for m in completed)
        assert pool.stats()['in_use'] == 0
        assert max(pool.generations) > 1
    finally:
        w.close()
        pool.close()


def test_dead_worker_releases_outstanding_lease_only_after_exit():
    w = start_mock_workers()
    pool = RGBPool(1, 100)
    lease = pool.copy(np.zeros((2, 3, 3), np.uint8))
    # Hold a loan without submitting; simulates a worker dying before dequeue.
    w.input_leases['detection', ('v', 0)] = lease.retain()
    lease.release()
    try:
        assert pool.stats()['in_use'] == 1
        process = w.processes['detection']
        process.terminate()
        process.join(5)
        list(w.poll())
        assert pool.stats()['in_use'] == 0
    finally:
        w.close()
        pool.close()


def test_worker_fifo_pixels_and_early_release_before_completion(monkeypatch):
    finished = Event()
    seen = []
    class Adapter:
        def __init__(self, *a, **k): pass
        def launch(self, job):
            seen.append((job['job_id'][1], job['rgb'].copy()))
            return SimpleNamespace(ready=SimpleNamespace(query=finished.is_set), times={})
        def finish(self, job, submission): return {}, {}
    monkeypatch.setattr(workers, 'TorchAdapter', Adapter)
    pool = RGBPool(2, 100)
    incoming, outgoing = queue.Queue(), queue.Queue()
    leases = [pool.copy(np.full((2, 3, 3), n, np.uint8)) for n in (7, 9)]
    for fid, lease in enumerate(leases):
        incoming.put(dict(job_id=['v', fid], priority=0, shared_rgb=lease.wire()))
    from threading import Thread
    thread = Thread(target=workers.worker_main, args=('detection', {}, dict(
        backend='torchscript', weights_dir='', priority_streams=[0], warmup_count=0), incoming, outgoing))
    thread.start()
    try:
        assert outgoing.get(timeout=5)['type'] == 'ready'
        for _ in leases:
            message = outgoing.get(timeout=5)
            assert message['type'] == 'input_released'
            leases[message['job_id'][1]].release()
        assert not finished.is_set() and pool.stats()['in_use'] == 0
        assert [fid for fid, _ in seen] == [0, 1]
        assert [int(image[0, 0, 0]) for _, image in seen] == [7, 9]
    finally:
        finished.set()
        incoming.put(None)
        thread.join(5)
        for lease in leases: lease.release()
        pool.close()
    assert not thread.is_alive()


@pytest.mark.skipif(os.getenv('MEC_TEST_CUDA') != '1', reason='opt-in CUDA acceptance')
def test_cuda_shared_input_matches_array_input_and_survives_slot_reuse(tmp_path):
    import torch
    class Boxes(torch.nn.Module):
        def forward(self, x):
            score = x.mean().float()
            return torch.stack([score*0, score*0, score, score, score, score*0]).reshape(1, 1, 6)
    torch.jit.trace(Boxes(), torch.ones(1, 3, 32, 32)).save(str(tmp_path/'boxes.pt'))
    spec = dict(application='detection', model='tiny', input_hw=[32, 32], artifact='boxes.pt', confidence=0)
    adapter = workers.TorchAdapter({'tiny': spec}, tmp_path, [0], mps_enabled=False)
    for _ in range(5):
        adapter.infer(dict(priority=0, configuration=spec, rgb=np.zeros((16, 32, 3), np.uint8)))
    pool = RGBPool(1, 4096)
    try:
        for value in (0, 37, 255):
            image = np.full((16, 32, 3), value, np.uint8)
            reference, _ = adapter.infer(dict(priority=0, configuration=spec, rgb=image))
            lease = pool.copy(image)
            mapping = RGBMapping(lease.wire())
            job = dict(priority=0, configuration=spec, rgb=mapping.image)
            # Keep the upload pending while we overwrite its original shared slot.
            with torch.cuda.stream(adapter.streams[0]):
                torch.cuda._sleep(200_000_000)
            submitted = adapter.launch(job)
            assert not submitted.ready.query()
            job.pop('rgb')
            mapping.close()
            lease.release()
            replacement = pool.copy(np.full_like(image, 123))
            result, _ = adapter.finish(job, submitted)
            assert result == reference
            replacement.release()
    finally:
        pool.close()
