"""One serialized ByteTrack state per video. Missing original frames advance time."""

from types import SimpleNamespace
import numpy as np


class Tracker:
    def __init__(self, fps, retention_s, mock=False):
        self.last = -1
        self.fps = fps
        self.mock = mock
        if not mock:
            from ultralytics.trackers.byte_tracker import BYTETracker

            # Ultralytics 8.4 reads timing retention from the tracker configuration.
            self.tracker = BYTETracker(
                SimpleNamespace(
                    track_high_thresh=0.25,
                    track_low_thresh=0.1,
                    new_track_thresh=0.25,
                    track_buffer=round(retention_s * fps),
                    match_thresh=0.8,
                    fuse_score=True,
                )
            )
            # Frame-based retention is explicitly original-video time, independent of detector completions.
            self.tracker.max_time_lost = round(retention_s * fps)

    # Original frame-number gaps age tracks; detector completion order is not time.
    def update(self, frame_id, payload=None):
        if frame_id <= self.last:
            raise ValueError("tracker cannot move backward")
        if self.mock:
            self.last = frame_id
            return dict(payload or {}, schema="tracking.v1", tracks=[])
        from ultralytics.engine.results import Boxes

        empty = Boxes(np.empty((0, 6), dtype=np.float32), (1, 1))
        # Each missing interval performs prediction/aging without detections.
        # Large gaps clear expired state before bounded prediction work.
        gap = frame_id - self.last - 1
        if gap > self.tracker.max_time_lost + 1:
            self.tracker.reset()
            self.tracker.frame_id = frame_id
            gap = 0
        for _ in range(gap):
            self.tracker.update(empty)
        dets = (payload or {}).get("detections", [])
        arr = np.asarray(
            [d["box"] + [d["score"], d["class_id"]] for d in dets], dtype=np.float32
        ).reshape(-1, 6)
        result = self.tracker.update(
            Boxes(
                arr, ((payload or {}).get("height", 1), (payload or {}).get("width", 1))
            )
        )
        self.last = frame_id
        if payload is None:
            return None
        tracks = [
            dict(
                box=list(map(float, row[:4])),
                track_id=int(row[4]),
                score=float(row[5]),
                class_id=int(row[6]),
            )
            for row in result
        ]
        return dict(payload, schema="tracking.v1", tracks=tracks)
