from collections import deque
from statistics import median, mean
import math


class History:
    def __init__(self, initial, statistic="median"):
        if not math.isfinite(initial) or initial < 0:
            raise ValueError("compatible finite nonnegative initialization required")
        self.initial, self.statistic = initial, statistic
        self.values = deque(maxlen=10)

    def add(self, value, completed=True):
        if completed and math.isfinite(value) and value >= 0:
            self.values.append(value)

    @property
    def value(self):
        if len(self.values) < 10:
            return self.initial
        return (mean if self.statistic == "mean" else median)(self.values)


class NetworkFilter:
    """D42–D50. All times seconds, variances seconds², q seconds²/second."""

    def __init__(self, profile):
        self.profile = profile
        self.reset(0)

    def reset(self, now):
        p = self.profile
        self.mu, self.P, self.last = p["mean_s"], p["P0_s2"], now
        self.reset_at = now
        self.last_observation = None
        self.seen = set()

    def propagate(self, now):
        if (
            now
            - (
                self.last_observation
                if self.last_observation is not None
                else self.reset_at
            )
            >= 5
        ):
            self.reset(now)
        self.P += self.profile["q_s2_per_s"] * max(0, now - self.last)
        self.last = now

    def observe(
        self, identity, size_bytes, throughput_bytes_s, bitrate_bps, now, valid=True
    ):
        if self.last_observation is not None and now - self.last_observation >= 5:
            self.reset(now)
        self.propagate(now)
        if (
            not valid
            or identity in self.seen
            or throughput_bytes_s <= 0
            or bitrate_bps <= 0
        ):
            return False
        y = size_bytes / throughput_bytes_s / (bitrate_bps / 1e6)
        if not math.isfinite(y) or y < 0:
            return False
        R = self.profile["R_s2"]
        gain = self.P / (self.P + R)
        self.mu += gain * (y - self.mu)
        self.P *= 1 - gain
        self.last_observation = now
        self.seen.add(identity)
        # Run-scoped frame dedup is also enforced by the frame registry.
        if len(self.seen) > 4096:
            self.seen = {identity}
        return True

    def variance(self, now):
        self.propagate(now)
        return self.P + self.profile["V_frame_s2"]
