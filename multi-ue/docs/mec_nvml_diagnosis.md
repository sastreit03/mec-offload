# NVML diagnosis and deployment validation


## NVML audit

Produced on 23 November 2026. This document describes the NVML errors encountered and their fix. NVML is used to read GPU telemetry on the MEC Spark.

Read-only probes inside `multi-ue-mec` on MEC `spark-9d60` (192.168.71.130)
reported NVIDIA GB10, driver 580.173.02: utilization succeeds (gpu=0, memory=0 at
probe time); `nvmlDeviceGetMemoryInfo` raises `NVMLError_NotSupported(3)`.
The campaign has 28,793 all-invalid samples because the previous collector
wrapped both operations in one try block. The collector now calls only
`nvmlDeviceGetUtilizationRates`; it no longer queries unsupported NVML framebuffer
memory info. `gpu_utilization_percent` retains GPU activity and
`memory_utilization_percent` retains memory read/write activity (neither memory
capacity occupied nor percentage of peak memory bandwidth).

For capacity, the collector reads `MemTotal` and `MemAvailable` directly from
`/proc/meminfo`, the source used by `free -h`, without spawning a subprocess or
parsing rounded human-readable values. It converts Linux kB to bytes (×1024) and
records `system_memory_total_bytes` and `system_memory_available_bytes` in the
GPU resource sample. `system_memory_scope` explicitly labels these as host shared
CPU/GPU RAM, not GPU-only or MEC-process-only memory. `MemAvailable` estimates
memory available for new allocations, including reclaimable memory; it is not
just `MemFree`. These host values do not represent a container's cgroup limit.

`operations.nvmlDeviceGetUtilizationRates` and `operations.proc_meminfo` carry
independent validity/error status. Top-level `valid` means both succeeded;
`partial=true` means one succeeded. Missing or malformed capacity data remains
absent, never zero. System memory is still sampled when NVML initialization or
utilization fails. The collector does not create a CUDA context.

Reproduce the supported measurements read-only on MEC (no restart required):

```bash
free -h
docker exec -i multi-ue-mec python - <<'PYCODE'
from pathlib import Path
import pynvml as n
n.nvmlInit()
try:
    u = n.nvmlDeviceGetUtilizationRates(n.nvmlDeviceGetHandleByIndex(0))
    print(dict(gpu_activity_percent=u.gpu, memory_activity_percent=u.memory))
finally:
    n.nvmlShutdown()
print('\n'.join(line for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith(('MemTotal:', 'MemAvailable:'))))
PYCODE
```

Deployment is **not performed by this change**. Transfer the reviewed changed
`multi-ue/mec/src/mec_server/` sources to the MEC checkout and build a separately
tagged image there, leaving the running container intact:

```bash
cd ~/mec-offload-ad/multi-ue
docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:timing .
```

After operator approval, recreate the MEC service with that image using the
existing deployment's mounts, command, configuration and MPS/graph settings.
Do not change these settings to validate instrumentation. Copy updated tools to
the analysis checkout. Optional environment variables `MEC_TRACE_HANDOFF=1` and
`MEC_TRACE_GC=1` require inclusion in the worker container environment; compare
traced versus untraced runs before assuming their overhead is negligible.