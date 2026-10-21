from dataclasses import dataclass, field
from math import isfinite


@dataclass
class FrameDemand:
    frame_id: int
    start_ns: int
    deadline_ns: int | None
    total: int
    actual: bool = False
    config: int = 0
    confirmed: int = 0
    estimated: float = 0.0
    scheduled: int = 0
    reception_complete: bool = False
    progress_seq: int = -1
    size_revision: int = 0
    source: str = "type-profile"

    @property
    def remaining(self):
        return max(0.0, self.total - max(self.confirmed, self.estimated))


class Ledger:
    def __init__(self, max_frames=8192):
        self.frames = {}
        self.closed = []  # compressed inclusive completed-ID intervals
        self.watermark = -1
        self.max_frames = max_frames
        self.valid = True

    def descriptor(
        self, frame_id, start_ns, deadline_ns, size, actual, config=0, revision=0
    ):
        if deadline_ns is not None and (
            isinstance(deadline_ns, bool) or not isinstance(deadline_ns, int)
        ):
            raise ValueError("invalid deadline clock value")
        if (
            any(
                isinstance(v, bool) or not isinstance(v, int)
                for v in [frame_id, start_ns, size, config, revision]
            )
            or frame_id < 0
            or size <= 0
            or start_ns < 0
        ):
            raise ValueError("invalid demand descriptor")
        if frame_id <= self.watermark or any(
            a <= frame_id <= b for a, b in self.closed
        ):
            return
        old = self.frames.get(frame_id)
        if old:
            if (
                old.actual
                and actual
                and (old.total != size or old.config != config)
                and revision <= old.size_revision
            ):
                raise ValueError("conflicting actual descriptor")
            if old.actual and not actual:
                return
            if actual or revision > old.size_revision:
                if old.confirmed > size:
                    raise ValueError("size correction below confirmed bytes")
                old.total = size
                old.actual = actual
                old.size_revision = revision
                old.config = config
                old.start_ns = start_ns
                old.deadline_ns = deadline_ns
                old.source = "descriptor" if actual else "type-profile"
            return
        if not self.valid or len(self.frames) >= self.max_frames:
            self.valid = False
            raise ValueError("demand_capacity_exceeded_use_PF; no radio cancellation")
        self.frames[frame_id] = FrameDemand(
            frame_id, start_ns, deadline_ns, size, actual, config
        )

    def progress(self, frame_id, unique, sequence, complete=False):
        f = self.frames.get(frame_id)
        if f is None:
            return
        if unique < 0 or unique > f.total:
            raise ValueError("invalid received-byte progress")
        if sequence < f.progress_seq:
            return
        if sequence == f.progress_seq and unique != f.confirmed:
            raise ValueError("conflicting progress sequence")
        if unique < f.confirmed:
            return
        if complete and (not f.actual or unique != f.total):
            raise ValueError("completion requires full actual size")
        f.confirmed = unique
        f.progress_seq = sequence
        # Receipt and estimated radio delivery overlap. Never add their credits.
        f.estimated = max(f.estimated, float(unique))
        if complete:
            del self.frames[frame_id]
            merged = []
            for a, b in sorted(self.closed + [(frame_id, frame_id)]):
                if merged and a <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            self.closed = merged
            while self.closed and self.closed[0][0] <= self.watermark + 1:
                self.watermark = max(self.watermark, self.closed.pop(0)[1])
            if len(self.closed) >= self.max_frames:
                self.valid = False

    def estimated_service(self, capacity_bytes, payload_fraction):
        """Provisional FIFO attribution only; mixed bearer traffic remains uncertain."""
        if not isfinite(capacity_bytes) or capacity_bytes < 0:
            raise ValueError("invalid scheduled capacity")
        if not isfinite(payload_fraction) or not 0 <= payload_fraction <= 1:
            raise ValueError("conversion fraction")
        left = capacity_bytes * payload_fraction
        for f in sorted(self.frames.values(), key=lambda f: (f.start_ns, f.frame_id)):
            if left <= 0:
                break
            used = min(f.remaining, left)
            f.estimated += used
            left -= used
        # Scheduled capacity remains distinct; never becomes a confirmed receipt.
        if self.frames:
            min(
                self.frames.values(), key=lambda f: (f.start_ns, f.frame_id)
            ).scheduled += capacity_bytes

    def snapshot(self, now_ns):
        fs = sorted(self.frames.values(), key=lambda f: (f.start_ns, f.frame_id))
        oldest = fs[0] if fs else None
        deadlines = [f.deadline_ns for f in fs if f.deadline_ns is not None]
        return {
            "valid": self.valid,
            "count": len(fs),
            "remaining_bytes": sum(f.remaining for f in fs),
            "earliest_deadline_ns": min(deadlines) if deadlines else None,
            "overdue_bytes": sum(
                f.remaining
                for f in fs
                if f.deadline_ns is not None and f.deadline_ns < now_ns
            ),
            "oldest": None
            if oldest is None
            else dict(
                frame_id=oldest.frame_id,
                total_bytes=oldest.total,
                remaining_bytes=oldest.remaining,
                actual=oldest.actual,
                confirmed_bytes=oldest.confirmed,
                estimated_unique_bytes=oldest.estimated,
                scheduled_tbs_bytes=oldest.scheduled,
                start_ns=oldest.start_ns,
                deadline_ns=oldest.deadline_ns,
                source=oldest.source,
            ),
        }
