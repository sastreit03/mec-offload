import sys
from types import SimpleNamespace

import pytest

from mec_server import cpu


def test_cpu_sampler_uses_nonblocking_intervals_and_reprimes_after_failure(monkeypatch):
    readings = iter([0, [0, 0], 28.4, [41.2, 15.6], OSError('unavailable'),
                     0, [0, 0], 40, [60, 20]])
    calls = []

    def cpu_percent(interval=None, percpu=False):
        calls.append((interval, percpu))
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(cpu_percent=cpu_percent))
    clock = iter([1_000_000_000, 1_100_200_000, 1_200_000_000,
                  1_300_000_000, 1_450_000_000])
    monkeypatch.setattr(cpu.time, 'monotonic_ns', lambda: next(clock))
    sampler = cpu.HostCpuSampler()
    baseline = sampler.sample()
    assert baseline['valid'] is False
    assert 'utilization_percent' not in baseline
    sample = sampler.sample()
    assert sample['valid'] is True
    assert sample['interval_s'] == pytest.approx(.1002)
    assert sample['utilization_percent'] == 28.4
    assert sample['per_cpu_utilization_percent'] == [41.2, 15.6]
    assert sample['scope'] == 'host; not MEC process only'
    assert sampler.sample()['valid'] is False
    assert sampler.sample()['reason'] == 'cpu_baseline_pending'
    assert sampler.sample()['interval_s'] == pytest.approx(.15)
    assert all(interval is None for interval, _ in calls)


def test_cpu_sampler_missing_dependency_is_explicit(monkeypatch):
    monkeypatch.setitem(sys.modules, 'psutil', None)
    sample = cpu.HostCpuSampler().sample()
    assert sample['valid'] is False
    assert sample['reason']
    assert 'utilization_percent' not in sample


def test_resource_report_records_and_forwards_cpu_in_existing_event(monkeypatch):
    import asyncio
    from mec_server.engine import Engine

    sample = dict(valid=True, utilization_percent=28.4,
                  per_cpu_utilization_percent=[41.2, 15.6], interval_s=.1)
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(ran_telemetry={'report_interval_ms': 100}, applications=(), priority_streams=())
    engine.videos = {}
    engine.workers = SimpleNamespace(ready={})
    engine.gpu = SimpleNamespace(sample=lambda: {'valid': False})
    engine.cpu = SimpleNamespace(sample=lambda: sample)
    forwarded, recorded = [], []
    engine.aware = lambda kind, **data: forwarded.append((kind, data))
    engine.record = SimpleNamespace(event=lambda kind, **data: recorded.append((kind, data)))
    waits = []

    async def sleep(delay):
        waits.append(delay)
        if len(waits) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.resource_reports())
    assert len(recorded) == len(forwarded) == 1
    assert recorded[0][0] == forwarded[0][0] == 'mec_resources'
    assert recorded[0][1]['snapshot']['cpu'] == sample
    assert forwarded[0][1]['cpu'] == sample
    assert 0 <= waits[0] <= .1
