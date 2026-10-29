"""Low-rate device telemetry that does not create a CUDA context."""

import time


class DeviceGpuSampler:
    def __init__(self, device_index=0):
        self.device_index = device_index
        self.nvml = None
        self.handle = None
        self.error = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self.nvml = pynvml
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
        except Exception as ex:
            # Aetheredge: Missing NVML must not prevent the MEC from serving frames.
            self.error = repr(ex)

    def sample(self):
        measured = time.monotonic_ns()
        if self.handle is None:
            return dict(
                valid=False,
                scope="GPU device; not per-stream or kernel residency",
                device_index=self.device_index,
                measurement_monotonic_ns=measured,
                reason=self.error,
            )
        try:
            util = self.nvml.nvmlDeviceGetUtilizationRates(self.handle)
            memory = self.nvml.nvmlDeviceGetMemoryInfo(self.handle)
            return dict(
                valid=True,
                scope="GPU device; not per-stream or kernel residency",
                device_index=self.device_index,
                measurement_monotonic_ns=measured,
                gpu_utilization_percent=util.gpu,
                memory_utilization_percent=util.memory,
                memory_used_bytes=memory.used,
                memory_total_bytes=memory.total,
            )
        except Exception as ex:
            return dict(
                valid=False,
                scope="GPU device; not per-stream or kernel residency",
                device_index=self.device_index,
                measurement_monotonic_ns=measured,
                reason=repr(ex),
            )

    def close(self):
        if self.nvml is not None:
            try:
                self.nvml.nvmlShutdown()
            except Exception:
                pass
            self.nvml = None
            self.handle = None
