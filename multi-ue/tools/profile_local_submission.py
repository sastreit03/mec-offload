#!/usr/bin/env python3
"""Time the production Linux encoder and RTP submission over IPv4 loopback."""

import argparse
import csv
import hashlib
import json
import multiprocessing as mp
import platform
import secrets
import socket
import sys
import time
import uuid
from pathlib import Path

from profile_encoding import videos_in
from profile_encoding_speed import percentile, sha256, write_csv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'contracts/python'))
from mec_contracts.rtp import Assembly, Descriptor, canonical_au, packetize, parse_packet


def receiver_main(control, video_id, receive_buffer):
    """Separate process avoids sharing the sender's Python GIL; never sends data."""
    sock = None
    try:
        # Create socket to receive at 127.0.0.1
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(.02)
        control.send(dict(port=sock.getsockname()[1],
                          receive_buffer_bytes=sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)))
        frames, active = {}, {}
        packets = errors = 0
        expected = None
        drain_deadline = None
        while True:
            if control.poll():
                command = control.recv()
                if command['op'] == 'stop':
                    return
                expected = command['packets']
                drain_deadline = time.monotonic() + command['timeout']
            if expected is not None and (packets >= expected or time.monotonic() >= drain_deadline):
                break
            try:
                raw = sock.recv(65535)
            except socket.timeout:
                continue
            received = time.monotonic_ns()
            packets += 1
            try:
                packet = parse_packet(raw)
                if packet.descriptor.video_id != video_id:
                    raise ValueError('unexpected stream ID')
                fid = packet.descriptor.frame_id
                if fid not in frames:
                    frames[fid] = dict(packets=0, udp_bytes=0, first_ns=received,
                                       last_ns=received, complete=False, digest=hashlib.sha256())
                    active[fid] = Assembly(packet.descriptor)
                row = frames[fid]
                row['packets'] += 1
                row['udp_bytes'] += len(raw)
                row['last_ns'] = received
                row['digest'].update(raw)
                if row['complete']:
                    raise ValueError('packet after completed frame')
                if active[fid].add(packet):
                    row['complete'] = True
                    del active[fid]  # Release payload memory after assembly completes.
            except (ValueError, IndexError):
                errors += 1
        for row in frames.values():
            row['sha256'] = row.pop('digest').hexdigest()
        control.send(dict(frames=frames, received_packets=packets, receiver_errors=errors))
    except BaseException as error:
        try:
            control.send(dict(error=repr(error)))
        except (BrokenPipeError, EOFError):
            pass
    finally:
        if sock is not None:
            sock.close()
        control.close()


class LocalReceiver:
    def __init__(self, video_id, receive_buffer):
        context = mp.get_context('spawn')
        self.control, child = context.Pipe()
        self.process = context.Process(target=receiver_main,
                                       args=(child, video_id, receive_buffer), daemon=True)
        self.process.start()
        child.close()
        try:
            self.settings = self.result(10)
        except BaseException:
            self.close()
            raise

    def result(self, timeout):
        if not self.control.poll(timeout):
            raise RuntimeError('local receiver timed out')
        result = self.control.recv()
        if 'error' in result:
            raise RuntimeError('local receiver: ' + result['error'])
        return result

    def finish(self, packets, timeout):
        self.control.send(dict(op='finish', packets=packets, timeout=timeout))
        return self.result(timeout + 10)

    def close(self):
        if self.process.is_alive():
            try:
                self.control.send(dict(op='stop'))
            except (BrokenPipeError, OSError):
                pass
            self.process.join(1)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join()
        else:
            self.process.join()
        self.control.close()


def attach_receipts(rows, receipt, origin):
    """Require packet count, byte count, assembly, and exact datagram hash agreement."""
    for row in rows:
        received = receipt['frames'].get(row['frame_id'], {})
        row.update(received_packets=received.get('packets', 0),
                   received_udp_bytes=received.get('udp_bytes', 0),
                   receiver_assembly_complete=received.get('complete', False),
                   receiver_first_ns=received.get('first_ns', origin) - origin if received else '',
                   receiver_last_ns=received.get('last_ns', origin) - origin if received else '')
        row['receipt_verified'] = (
            received.get('complete', False)
            and received.get('packets') == row['submitted_packets']
            and received.get('udp_bytes') == row['submitted_udp_bytes']
            and received.get('sha256') == row['submitted_sha256'])


def measure_stream(video, bitrate, args, cv2, encoder_class):
    setup_start = time.monotonic_ns()
    capture = cv2.VideoCapture(str(video))
    encoder = receiver = sock = None
    try:
        if not capture.isOpened():
            raise RuntimeError(f'unreadable video: {video}')
        video_id = str(uuid.uuid4())
        receiver = LocalReceiver(video_id, args.receive_buffer_bytes)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.connect(('127.0.0.1', receiver.settings['port']))
        encoder = encoder_class(args.width, args.height, args.fps, bitrate)
        sequence, rtp_base, ssrc = secrets.randbits(16), secrets.randbits(32), secrets.randbits(32)
        origin = time.monotonic_ns()
        rows = []
        while True:
            fid = len(rows)
            release = origin + round(fid * 1e9 / args.fps)
            deadline = origin + round((fid + 1) * 1e9 / args.fps)
            while True:
                delay = release - time.monotonic_ns()
                if delay <= 0:
                    break
                time.sleep(delay / 1e9)
            read_start = time.monotonic_ns()
            ok, bgr = capture.read()
            read_end = time.monotonic_ns()
            if not ok:
                break
            bgr = cv2.resize(bgr, (args.width, args.height))
            resize_end = time.monotonic_ns()
            nals = encoder.frame(bgr, fid, args.fps,
                                 keyframe=fid % args.gop_frames == 0, bitrate=bitrate)
            encode_end = time.monotonic_ns()
            if not nals:
                raise RuntimeError(f'empty encoder output: {video} frame {fid}')
            idr = any(n[0] & 31 == 5 for n in nals)
            if fid % args.gop_frames == 0 and not idr:
                raise RuntimeError(f'missing requested IDR: {video} frame {fid}')
            # Same descriptor and packetize/list/send sequence as the production Streamer.
            descriptor = Descriptor(video_id, 1, fid, fid // args.gop_frames, 1, 1,
                                    1 if idr else 3, len(canonical_au(nals)),
                                    capture_mono_ns=read_end, tx_mono_ns=encode_end, flags=3)
            packetize_start = time.monotonic_ns()
            packets = list(packetize(nals, descriptor, sequence,
                                    rtp_base + round(fid * 90000 / args.fps), ssrc, args.mtu))
            packetize_end = time.monotonic_ns()
            send_start = time.monotonic_ns()
            for packet in packets:
                if sock.send(packet) != len(packet):
                    raise RuntimeError('short UDP submission')
            send_end = time.monotonic_ns()
            sequence = (sequence + len(packets)) & 65535
            # Verification bookkeeping is outside submission timing; it can affect next release.
            digest = hashlib.sha256()
            for packet in packets:
                digest.update(packet)
            rows.append(dict(
                video=str(video), video_id=video_id, bitrate_bps=bitrate, frame_id=fid,
                gop_id=fid // args.gop_frames, gop_frame=fid % args.gop_frames,
                frame_type='I' if idr else 'P', encoded_bytes=descriptor.encoded_bytes,
                submitted_packets=len(packets), submitted_udp_bytes=sum(map(len, packets)),
                submitted_sha256=digest.hexdigest(), scheduled_release_ns=release-origin,
                deadline_ns=deadline-origin, read_start_ns=read_start-origin,
                read_end_ns=read_end-origin, resize_end_ns=resize_end-origin,
                encode_end_ns=encode_end-origin, packetize_start_ns=packetize_start-origin,
                packetize_end_ns=packetize_end-origin, send_start_ns=send_start-origin,
                send_end_ns=send_end-origin, decode_ns=read_end-read_start,
                resize_ns=resize_end-read_end, encoder_call_ns=encode_end-resize_end,
                descriptor_ns=packetize_start-encode_end,
                packetization_ns=packetize_end-packetize_start,
                socket_submission_ns=send_end-send_start, processing_ns=send_end-read_start,
                start_lateness_ns=max(0, read_start-release),
                release_to_submission_ns=send_end-release,
                deadline_lateness_ns=max(0, send_end-deadline), deadline_missed=send_end>deadline))
        if not rows:
            raise RuntimeError(f'empty video: {video}')
        receipt = receiver.finish(sum(r['submitted_packets'] for r in rows), args.drain_timeout)
        attach_receipts(rows, receipt, origin)
        info = dict(setup_ns=origin-setup_start, **receiver.settings,
                    send_buffer_bytes=sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
                    received_packets=receipt['received_packets'], receiver_errors=receipt['receiver_errors'],
                    unexpected_frames=len(set(receipt['frames']) - {r['frame_id'] for r in rows}))
        return rows, info
    finally:
        try:
            if encoder is not None:
                encoder.close()
        finally:
            capture.release()
            if sock is not None:
                sock.close()
            if receiver is not None:
                receiver.close()


def summarize(rows, info, fps):
    count = len(rows)
    misses = sum(r['deadline_missed'] for r in rows)
    verified = sum(r['receipt_verified'] for r in rows)
    result = dict(video=rows[0]['video'], bitrate_bps=rows[0]['bitrate_bps'], frames=count,
                  fps=fps, frame_budget_ms=1000/fps, setup_ms=info['setup_ns']/1e6,
                  media_duration_s=count/fps, elapsed_to_last_submission_s=rows[-1]['send_end_ns']/1e9,
                  submitted_packets=sum(r['submitted_packets'] for r in rows),
                  submitted_udp_bytes=sum(r['submitted_udp_bytes'] for r in rows),
                  received_packets=info['received_packets'], verified_frames=verified,
                  unverified_frames=count-verified, receiver_errors=info['receiver_errors'],
                  unexpected_frames=info['unexpected_frames'],
                  receive_buffer_bytes=info['receive_buffer_bytes'], send_buffer_bytes=info['send_buffer_bytes'],
                  deadline_misses=misses, deadline_miss_pct=100*misses/count,
                  all_frames_submitted_by_deadline=misses == 0,
                  all_receipts_verified=verified == count and info['receiver_errors'] == 0
                  and info['unexpected_frames'] == 0,
                  final_deadline_lateness_ms=rows[-1]['deadline_lateness_ns']/1e6)
    for field in ('decode', 'resize', 'encoder_call', 'descriptor', 'packetization',
                  'socket_submission', 'processing', 'start_lateness',
                  'release_to_submission', 'deadline_lateness'):
        values = [r[field + '_ns']/1e6 for r in rows]
        for suffix, value in [('mean', sum(values)/count), ('p95', percentile(values, .95)),
                              ('p99', percentile(values, .99)), ('max', max(values))]:
            result[field + '_' + suffix + '_ms'] = value
    return result


def main(argv=None):
    # Read command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video-dir', type=Path, required=True)
    parser.add_argument('--bitrate', type=int, action='append', dest='bitrates', required=True)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--gop-frames', type=int, default=30)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-videos', type=int, default=8)
    parser.add_argument('--mtu', type=int, default=1200)
    parser.add_argument('--receive-buffer-bytes', type=int, default=4*1024*1024)
    parser.add_argument('--drain-timeout', type=float, default=2)
    args = parser.parse_args(argv)

    # Validate command line arguments
    if min(*args.bitrates, args.width, args.height, args.fps, args.gop_frames,
           args.expected_videos, args.receive_buffer_bytes) <= 0:
        parser.error('rates, dimensions, GOP, video count, and buffer size must be positive')
    if args.width % 2 or args.height % 2 or not 256 <= args.mtu <= 65507:
        parser.error('dimensions must be even; MTU must be 256..65507 UDP payload bytes')
    if not 0 < args.drain_timeout <= 60:
        parser.error('drain timeout must be positive and at most 60 seconds')
    videos = videos_in(args.video_dir)
    if len(videos) != args.expected_videos:
        parser.error(f'expected {args.expected_videos} videos, found {len(videos)}')
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('output directory must be new or empty')
    sys.path.insert(0, str(ROOT / 'ue/linux/src'))
    from mec_ue.media import Encoder
    import cv2

    # Save metadata in profile.json
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = dict(status='running', videos=list(map(str, videos)),
                    bitrates_bps=sorted(set(args.bitrates)), fps=args.fps, gop_frames=args.gop_frames,
                    width=args.width, height=args.height, mtu=args.mtu,
                    destination='127.0.0.1 (ephemeral port)', radio_included=False,
                    receiver='separate local process, production RTP parser and Assembly',
                    schedule='absolute FPS releases; retain every frame; catch up when late',
                    deadline='last socket.send returns by next scheduled frame release',
                    drain_timeout_s=args.drain_timeout, requested_receive_buffer_bytes=args.receive_buffer_bytes,
                    warmup_frames_excluded=0, timestamp_clock='monotonic_ns; relative to each stream origin',
                    platform=platform.platform(), python=sys.version, opencv=cv2.__version__,
                    encoder_source_sha256=sha256(ROOT / 'ue/linux/src/mec_ue/media.py'),
                    rtp_source_sha256=sha256(ROOT / 'contracts/python/mec_contracts/rtp.py'),
                    profiler_source_sha256=sha256(Path(__file__)))
    def save():
        (args.output / 'profile.json').write_text(json.dumps(metadata, indent=2)+'\n')
    save()
    summaries, counts = [], {}
    try:
        metadata['source_sha256'] = {str(v): sha256(v) for v in videos}
        save()
        with (args.output / 'frame-timings.csv').open('w', newline='') as output:
            writer = None
            # Check by bitrate and by video
            for rate in metadata['bitrates_bps']:
                for video in videos:
                    print(f'Profiling {video.name}: {rate} bps, {args.fps} FPS', flush=True)
                    rows, info = measure_stream(video, rate, args, cv2, Encoder)
                    if counts.setdefault(video, len(rows)) != len(rows):
                        raise RuntimeError(f'frame count changed across bitrates: {video}')
                    if writer is None:
                        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
                        writer.writeheader()
                    writer.writerows(rows)
                    output.flush()
                    summary = summarize(rows, info, args.fps)
                    summaries.append(summary)
                    write_csv(args.output / 'summary.csv', summaries)
                    print(f"  deadline misses={summary['deadline_misses']}/{len(rows)}; "
                          f"verified frames={summary['verified_frames']}/{len(rows)}; "
                          f"submission pipeline p99={summary['processing_p99_ms']:.2f} ms", flush=True)
        metadata.update(status='complete',
                        all_streams_meet_frame_deadlines=all(s['all_frames_submitted_by_deadline'] for s in summaries),
                        all_receipts_verified=all(s['all_receipts_verified'] for s in summaries))
    except BaseException as error:
        metadata.update(status='cancelled' if isinstance(error, KeyboardInterrupt) else 'failed', error=repr(error))
        raise
    finally:
        save()
    print(f'Results: {args.output}. Completion is not a timing or delivery PASS.')


if __name__ == '__main__':
    main()
