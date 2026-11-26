"""Verified campaign media and paced RTP submission."""
import hashlib
import re
import secrets
import socket
import struct
import time

from mec_contracts.rtp import Descriptor, packetize, split_annexb
from mec_ue.media import Streamer


def load_frames(folder, assignment, cfg):
    sha = assignment["media_sha256"].lower()
    if not re.fullmatch("[0-9a-f]{64}", sha):
        raise ValueError("Invalid source digest")
    width, height, fps, gop, rate = (cfg[k] for k in PROFILE)
    if rate != assignment["bitrate_bps"]:
        raise ValueError("Assigned and admitted bitrates differ")
    count = max(assignment["frame_count"], 8 * fps)
    key = f"px1-{sha}-{assignment['source_start_frame']}-{count}-{width}x{height}-{fps}-{gop}-{rate}"
    paths = list(folder.glob(key + "-*.pav"))
    if len(paths) != 1:
        raise ValueError(f"Expected one staged asset for {key}, found {len(paths)}")
    path = paths[0]
    digest = hashlib.sha256()
    with path.open("rb") as f:
        def read(n):
            data = f.read(n)
            if len(data) != n:
                raise ValueError("Truncated asset")
            digest.update(data)
            return data

        magic, size = struct.unpack(">4sH", read(6))
        if magic != b"PAV1" or read(size) != key.encode() or struct.unpack(">I", read(4))[0] != count:
            raise ValueError("Asset header differs from assignment")
        frames = []
        for i in range(count):
            size, flag = struct.unpack(">IB", read(5))
            if not 5 <= size <= 8 * 1024 * 1024:
                raise ValueError(f"Invalid frame size at {i}")
            nals = split_annexb(read(size))
            idr = any(n[0] & 31 == 5 for n in nals)
            if flag != int(idr) or (i % gop == 0 and not idr) or sum(4+len(n) for n in nals) != size:
                raise ValueError(f"Invalid access unit at {i}")
            frames.append((nals, size, idr))
        if f.read(1) or path.name != f"{key}-{digest.hexdigest()}.pav":
            raise ValueError("Asset digest or length mismatch")
    return path.name, frames


PROFILE = ("width", "height", "fps", "gop_frames", "bitrate_bps")


class Playback(Streamer):
    def __init__(self, *args, folder, assignment, register, **kwargs):
        super().__init__(*args, **kwargs)
        self.folder, self.assignment, self.register = folder, assignment, register

    def release(self):
        self.origin = time.monotonic_ns()
        super().release()

    def run(self):
        video = self.response["video_id"]
        try:
            cfg = self.current
            name, frames = load_frames(self.folder, self.assignment, cfg)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind((self.source_ip, 0))
                ep = self.response["endpoint"]
                sock.connect((ep["host"], ep["port"]))
                self.prepared = True
                self.event("media_prepared", dict(video_id=video, sender_mode="preencoded", asset=name, frame_count=len(frames)))
                self.release_event.wait()
                seq, base, ssrc = secrets.randbits(16), secrets.randbits(32), secrets.randbits(32)
                index = 0
                while not self.stopped.is_set():
                    due = self.origin + round(index * 1e9 / cfg["fps"])
                    if self.stopped.wait(max(0, due - time.monotonic_ns()) / 1e9):
                        break
                    with self.lock:
                        now = self.clock.unix_ns()
                        while self.pending and self.pending[0]["boundary_unix_ns"] <= now:
                            change = self.pending.pop(0)
                            if now - change["boundary_unix_ns"] > round(1e9 / cfg["fps"]):
                                self.event("plan_unapplied", dict(command_id=change["command_id"], reason="boundary_missed"))
                                continue
                            if any(change[k] != cfg[k] for k in PROFILE):
                                raise ValueError("Requested profile differs from staged encoding")
                            self.current = cfg = change
                            self.gop_frame = 0
                            self.gop_id += 1
                            if change["control_version"] > self.pause_version:
                                self.pause_at = None
                        paused = self.pause_at and self.pause_at <= now
                    if paused:
                        index += 1
                        continue
                    if index >= len(frames):
                        raise ValueError("Staged asset ended before scenario_stop")
                    nals, size, idr = frames[index]
                    if self.gop_frame == 0 and not idr:
                        raise ValueError("Staged frame cannot satisfy commanded keyframe")
                    capture = time.monotonic_ns()
                    valid = self.clock.valid()
                    tx = time.monotonic_ns()
                    self.register(video, index, tx)
                    d = Descriptor(video, self.response["media_epoch"], index, self.gop_id,
                                   cfg["config_version"], cfg["plan_version"], 1 if idr else 3,
                                   size, capture, tx, self.clock.to_unix(capture) if valid else 0,
                                   self.clock.to_unix(tx) if valid else 0, 15 if valid else 3)
                    for packet in packetize(nals, d, seq, base + round(index*90000/cfg["fps"]), ssrc, self.response["rtp"]["mtu"]):
                        if self.stopped.is_set():
                            return
                        final = packet[1] & 128
                        sock.send(packet)
                        if final:
                            last = time.monotonic_ns()
                        seq = (seq + 1) & 65535
                    self.frames_fully_sent += 1
                    self.last_frame = index
                    self.event("transmitted", dict(video_id=video, frame_id=index, tx_mono_ns=tx,
                               scheduled_release_mono_ns=due, last_udp_send_return_mono_ns=last,
                               capture_mono_ns=capture, capture_origin="playback_release",
                               sender_mode="preencoded", idr=idr, gop_frame=self.gop_frame, encoder_pts_us=round(index*1e6/cfg["fps"]),
                               source_frame_index=self.source_start_frame + index, encoded_bytes=size,
                               config_version=d.config_version, plan_version=d.plan_version, frame_type=d.frame_type))
                    index += 1
                    self.gop_frame = (self.gop_frame + 1) % cfg["gop_frames"]
                    if self.gop_frame == 0:
                        self.gop_id += 1
        except Exception as exc:
            self.event("sender_error", dict(video_id=video, reason=str(exc)))
        finally:
            self.event("media_end", dict(video_id=video, final_frame_id=self.last_frame,
                       frames_fully_sent=self.frames_fully_sent,
                       end_reason="scenario_stop" if self.stopped.is_set() else "sender_error"))
