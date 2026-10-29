import sys
from types import SimpleNamespace

from mec_server.gpu import DeviceGpuSampler


def test_device_gpu_sampler_labels_device_scope(monkeypatch):
    fake = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetHandleByIndex=lambda index: ("gpu", index),
        nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=37, memory=12),
        nvmlDeviceGetMemoryInfo=lambda handle: SimpleNamespace(
            used=1024, total=4096
        ),
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    sampler = DeviceGpuSampler(0)
    sample = sampler.sample()
    sampler.close()

    assert sample["valid"] is True
    assert sample["gpu_utilization_percent"] == 37
    assert sample["memory_used_bytes"] == 1024
    assert "not per-stream or kernel residency" in sample["scope"]
