"""Host CPU utilization sampled without waiting for a measurement interval."""

import time


class HostCpuSampler:
    def __init__(self):
        self.previous_ns = None
        self.psutil = None
        self.error = None
        try:
            import psutil

            self.psutil = psutil
        except ImportError as ex:
            self.error = repr(ex)

    def sample(self):
        measured = time.monotonic_ns()
        result = dict(
            valid=False,
            scope="host; not MEC process only",
            measurement_monotonic_ns=measured,
            interval_s=None,
        )
        if self.psutil is None:
            return dict(result, reason=self.error)
        try:
            # Keep calls on the resource-reporting thread: psutil's nonblocking
            # baselines are thread-local. Never sleep inside the MEC event loop.
            overall = self.psutil.cpu_percent(interval=None)
            per_cpu = self.psutil.cpu_percent(interval=None, percpu=True)
        except Exception as ex:
            self.previous_ns = None
            return dict(result, reason=repr(ex))
        previous = self.previous_ns
        self.previous_ns = measured
        if previous is None:
            # The first call establishes baselines, not a measured idle interval.
            return dict(result, reason="cpu_baseline_pending")
        interval = (measured - previous) / 1e9
        if interval <= 0 or not per_cpu:
            return dict(result, reason="cpu_interval_unavailable")
        return dict(result, valid=True, interval_s=interval,
                    utilization_percent=overall,
                    per_cpu_utilization_percent=per_cpu)
