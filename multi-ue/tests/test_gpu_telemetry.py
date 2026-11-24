import sys
from types import SimpleNamespace

import pytest

from mec_server.gpu import DeviceGpuSampler


@pytest.fixture
def sampler(monkeypatch, tmp_path):
    def unsupported(handle):
        pytest.fail('Unsupported NVML memory-info query must never be called')
    fake = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetHandleByIndex=lambda index: index,
        nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=37, memory=12),
        nvmlDeviceGetMemoryInfo=unsupported,
    )
    monkeypatch.setitem(sys.modules, 'pynvml', fake)
    path = tmp_path / 'meminfo'
    path.write_text('MemTotal: 4096 kB\nMemFree: 100 kB\nMemAvailable: 1024 kB\n')
    instance = DeviceGpuSampler(0, path)
    yield instance
    instance.close()


def test_activity_and_shared_system_capacity(sampler):
    sample = sampler.sample()
    assert sample['valid'] is True and sample['partial'] is False
    assert sample['gpu_utilization_percent'] == 37
    assert sample['memory_utilization_percent'] == 12
    assert sample['system_memory_total_bytes'] == 4096 * 1024
    assert sample['system_memory_available_bytes'] == 1024 * 1024
    assert 'memory_used_bytes' not in sample and 'memory_total_bytes' not in sample
    assert 'not per-stream or kernel residency' in sample['scope']
    assert 'not capacity' in sample['memory_utilization_scope']
    assert 'not GPU-only' in sample['system_memory_scope']
    assert set(sample['operations']) == {'nvmlDeviceGetUtilizationRates', 'proc_meminfo'}


def test_nvml_failure_keeps_system_memory(sampler):
    def failed(handle):
        raise RuntimeError('utilization failed')
    sampler.nvml.nvmlDeviceGetUtilizationRates = failed
    sample = sampler.sample()
    assert sample['valid'] is False and sample['partial'] is True
    assert sample['system_memory_available_bytes'] == 1024 * 1024
    assert 'gpu_utilization_percent' not in sample
    assert 'utilization failed' in sample['operations']['nvmlDeviceGetUtilizationRates']['reason']
    sampler.handle = None
    sampler.error = 'initialization failed'
    sample = sampler.sample()
    assert sample['system_memory_total_bytes'] == 4096 * 1024
    assert sample['operations']['nvmlDeviceGetUtilizationRates']['reason'] == 'initialization failed'


@pytest.mark.parametrize('contents', [
    'MemTotal: 4096 kB\n',
    'MemTotal: bad kB\nMemAvailable: 1 kB\n',
    'MemTotal: 4 MB\nMemAvailable: 1 MB\n',
    'MemTotal: 1 kB\nMemAvailable: 2 kB\n',
])
def test_unavailable_memory_is_not_zero(sampler, contents):
    sampler.meminfo_path.write_text(contents)
    sample = sampler.sample()
    assert sample['valid'] is False and sample['partial'] is True
    assert sample['gpu_utilization_percent'] == 37
    assert 'system_memory_total_bytes' not in sample
    assert 'system_memory_available_bytes' not in sample
    assert sample['operations']['proc_meminfo']['valid'] is False


def test_unreadable_memory_keeps_activity(sampler):
    sampler.meminfo_path.unlink()
    sample = sampler.sample()
    assert sample['memory_utilization_percent'] == 12
    assert sample['operations']['proc_meminfo']['valid'] is False
