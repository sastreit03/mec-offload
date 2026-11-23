"""Thread-safe, FIFO handoff from one GStreamer receiver to its engine loop."""

from collections import deque
from threading import Lock


class ReceiverHandoff:
    batch_size = 32

    def __init__(self, loop, dispatch, overflow, notifications=1024, pictures=10):
        self.loop = loop
        self.dispatch = dispatch
        self.overflow = overflow
        self.notifications = notifications
        self.pictures = pictures
        self.lock = Lock()
        self.items = deque()
        self.decoded = 0
        self.scheduled = False
        self.peak = self.peak_decoded = self.dropped = 0
        self.losses = {}
        self.lost_count = 0

    def __call__(self, kind, data):
        with self.lock:
            if len(self.items) >= self.notifications or (
                kind == "decoded" and self.decoded >= self.pictures
            ):
                self.dropped += 1
                self.lost_count += 1
                # Report even if the FIFO cannot accept an error record. Keep
                # one identity per affected frame and never retain its pixels.
                d = data.get("descriptor")
                if d is not None:
                    self.losses[d.frame_id] = d
            else:
                self.items.append((kind, data))
                self.decoded += kind == "decoded"
                self.peak = max(self.peak, len(self.items))
                self.peak_decoded = max(self.peak_decoded, self.decoded)
            if not self.scheduled:
                self.scheduled = True
                self.loop.call_soon_threadsafe(self.drain)

    def drain(self):
        try:
            for _ in range(self.batch_size):
                with self.lock:
                    if self.lost_count:
                        count, losses = self.lost_count, self.losses
                        self.lost_count, self.losses = 0, {}
                        item = None
                    elif self.items:
                        item = self.items.popleft()
                        self.decoded -= item[0] == "decoded"
                    else:
                        break
                if item is None:
                    self.overflow(count, losses.values())
                else:
                    self.dispatch(*item)
        finally:
            with self.lock:
                if self.items or self.lost_count:
                    self.loop.call_soon(self.drain)
                else:
                    self.scheduled = False

    def stats(self):
        with self.lock:
            return dict(depth=len(self.items), decoded=self.decoded,
                        peak_notifications=self.peak, peak_decoded=self.peak_decoded,
                        overflow_notifications=self.dropped)
