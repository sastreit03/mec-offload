"""Low-rate device telemetry that does not create a CUDA context."""

import time
from pathlib import Path


class DeviceGpuSampler:
    def __init__(self, device_index=0, meminfo_path="/proc/meminfo"):
        self.device_index = device_index
        self.meminfo_path = Path(meminfo_path)
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
        result = dict(valid=False, scope="GPU device; not per-stream or kernel residency",
                      device_index=self.device_index, measurement_monotonic_ns=measured,
                      memory_utilization_scope="GPU memory read/write activity; not capacity used or peak bandwidth",
                      system_memory_scope="host shared CPU/GPU RAM; not GPU-only or MEC-process-only memory",
                      operations={})
        operation = "nvmlDeviceGetUtilizationRates"
        if self.handle is None:
            result["operations"][operation] = dict(valid=False, reason=self.error)
        else:
            try:
                util = self.nvml.nvmlDeviceGetUtilizationRates(self.handle)
                result.update(gpu_utilization_percent=util.gpu,
                              memory_utilization_percent=util.memory)
                result["operations"][operation] = dict(valid=True)
            except Exception as ex:
                result["operations"][operation] = dict(valid=False, reason=repr(ex))

        # Read the source used by free(1), avoiding a subprocess and rounded -h
        # output. Linux meminfo's kB values are converted to bytes (1024 bytes).
        # This remains available even when NVML initialization/utilization fails.
        try:
            values = {}
            for line in self.meminfo_path.read_text().splitlines():
                parts = line.split()
                if parts and parts[0] in ("MemTotal:", "MemAvailable:"):
                    if len(parts) != 3 or parts[2] != "kB":
                        raise ValueError("unexpected meminfo units")
                    values[parts[0][:-1]] = int(parts[1]) * 1024
            total, available = values["MemTotal"], values["MemAvailable"]
            if not 0 <= available <= total or total == 0:
                raise ValueError("invalid system memory totals")
            result.update(system_memory_total_bytes=total,
                          system_memory_available_bytes=available)
            result["operations"]["proc_meminfo"] = dict(valid=True)
        except Exception as ex:
            result["operations"]["proc_meminfo"] = dict(valid=False, reason=repr(ex))
        # Preserve valid's all-operations meaning; partial fields remain usable.
        result["valid"] = all(v["valid"] for v in result["operations"].values())
        result["partial"] = not result["valid"] and any(v["valid"] for v in result["operations"].values())
        if not result["valid"]:
            result["reason"] = "; ".join(f"{k}: {v['reason']}" for k,v in result["operations"].items() if not v["valid"])
        return result

    def close(self):
        if self.nvml is not None:
            try:
                self.nvml.nvmlShutdown()
            except Exception:
                pass
            self.nvml = None
            self.handle = None
