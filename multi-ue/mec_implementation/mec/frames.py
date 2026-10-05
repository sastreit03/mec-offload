"""The ONLY decoded-frame waiting queue; safe for GStreamer callback threads."""
import threading
from dataclasses import dataclass

import numpy as np
from .records import stamp


class FIFO:
    """Replace this small policy through queues.frame_policy; no scheduler edits."""
    def choose(self, frames: list, can_drop_frames=False):
        return 0


@dataclass
class Frame:
    frame_id: int
    rgb: np.ndarray
    capture_unix_ns: int
    tx_unix_ns: int
    times: dict
    job_seq: int = -1

    def record(self):
        return {"frame_id": self.frame_id, "job_seq": self.job_seq,
                "ue_capture_unix_ns": self.capture_unix_ns, "ue_tx_start_unix_ns": self.tx_unix_ns,
                "times": dict(self.times)}


class Ranges:
    """Compact received-ID intervals, including duplicate detection and final gaps."""
    def __init__(self):
        self.ranges = []

    def add(self, value: int) -> bool:
        for lo, hi in self.ranges:
            if lo <= value <= hi:
                return False
        entries = sorted(self.ranges + [(value, value)])
        merged = []
        for lo, hi in entries:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        self.ranges = merged
        return True

    def missing(self, final: int):
        cursor = 0
        for lo, hi in self.ranges:
            if lo > final:
                break
            if lo > cursor:
                yield cursor, min(lo - 1, final)
            cursor = max(cursor, hi + 1)
        if cursor <= final:
            yield cursor, final


class FrameQueue:
    def __init__(self, cap: int, policy, on_drop, can_drop_frames=False):
        self.cap, self.policy, self.on_drop = cap, policy, on_drop
        self.can_drop_frames = can_drop_frames
        self.lock = threading.RLock()
        self.frames = []
        self.received = Ranges()
        self.next_seq = 0
        self.closed = False
        self.final_frame_id = None
        self.last_dispatched_id = -1

    def put(self, frame: Frame):
        dropped = None
        with self.lock:
            if self.closed or (self.final_frame_id is not None and frame.frame_id > self.final_frame_id):
                return
            if not self.received.add(frame.frame_id):
                return
            frame.job_seq = self.next_seq
            self.next_seq += 1
            frame.times["queue_enter"] = stamp()
            # Jitter reordering belongs before this queue. A tardy decoded frame
            # cannot be fed backward to a stateful tracker.
            if frame.frame_id <= self.last_dispatched_id:
                dropped = (frame, "late_frame")
            else:
                self.frames.append(frame)
                self.frames.sort(key=lambda f: f.frame_id)
                if len(self.frames) > self.cap:
                    dropped = (self.frames.pop(0), "buffer_overflow")
        if dropped:
            self.on_drop(*dropped)

    def pop(self):
        skipped = []
        with self.lock:
            if not self.frames:
                return None
            index = self.policy.choose(self.frames, self.can_drop_frames) if self.can_drop_frames else 0
            if not isinstance(index, int) or not 0 <= index < len(self.frames):
                raise ValueError("Frame policy must select an existing frame index")
            skipped, self.frames = self.frames[:index], self.frames[index:]
            frame = self.frames.pop(0)
            self.last_dispatched_id = frame.frame_id
        for old in skipped:
            self.on_drop(old, "policy_skipped")
        return frame

    def discard(self, reason):
        with self.lock:
            self.closed = True
            frames, self.frames = self.frames, []
        for frame in frames:
            self.on_drop(frame, reason)

    def snapshot(self):
        with self.lock:
            return [f.record() for f in self.frames]

    def __len__(self):
        with self.lock:
            return len(self.frames)
