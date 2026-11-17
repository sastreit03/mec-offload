"""Collection integrity survives transient failures and accepts idle counters."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from muApp_mec.runtime import Runtime
from muApp_mec.telemetry import Window
from muApp_mec.legacy_wire import metrics_pb2


def runtime(tmp_path):
    cfg = yaml.safe_load((Path(__file__).parents[1] / 'config/edgeric.shadow.yaml').read_text())
    cfg['runs_dir'] = str(tmp_path)
    r = Runtime(cfg, SimpleNamespace(source_generation=1, report_sequence=1))
    r.attempt_id = 'attempt'
    r.window = Window(0, 100)
    r.associations.bindings['ue'] = SimpleNamespace(rnti=7)
    msg = metrics_pb2.UeMetrics(scheduling_counters_valid=True)
    r.metrics = {7: {f.name: getattr(msg, f.name) for f in msg.DESCRIPTOR.fields}}
    return r


def test_zero_counters_and_repeated_sequence(tmp_path):
    r = runtime(tmp_path)
    assert r._consume_ran_sample(1)
    r.adapter.report_sequence = 2
    assert r._consume_ran_sample(1.1)
    assert not r.telemetry_errors
    assert r._consume_ran_sample(1.2) is False
    r.latest_metric = 1.1
    r.check_telemetry_freshness(1.39)
    assert not r.telemetry_errors
    r.check_telemetry_freshness(1.41)
    r.latest_metric = 1.42
    r.check_telemetry_freshness(1.43)
    assert r.telemetry_errors['source'] == 'gnb_telemetry_stale'


@pytest.mark.parametrize('failure', ['missing', 'flag', 'negative', 'regression'])
def test_counter_failure_is_retained(tmp_path, failure):
    r = runtime(tmp_path)
    r.metrics[7]['new_data_prbs_total'] = 10
    r._consume_ran_sample(1)
    r.adapter.report_sequence = 2
    if failure == 'missing':
        del r.metrics[7]['new_data_prbs_total']
    elif failure == 'flag':
        r.metrics[7]['scheduling_counters_valid'] = False
    else:
        r.metrics[7]['new_data_prbs_total'] = -1 if failure == 'negative' else 9
    r._consume_ran_sample(1.1)
    assert 'ue' in r.telemetry_errors
    r.metrics[7]['scheduling_counters_valid'] = True
    r.metrics[7]['new_data_prbs_total'] = 11
    r.adapter.report_sequence = 3
    r._consume_ran_sample(1.2)
    assert 'ue' in r.telemetry_errors


def test_evidence_queue_and_write_failures_latch(tmp_path):
    async def exercise():
        r = runtime(tmp_path)
        r.log_queue = asyncio.Queue(maxsize=1)
        r.event('one')
        r.event('overflow')
        assert r.log_errors == 1
        def fail(_):
            raise OSError('disk full')
        r._append = fail
        task = asyncio.create_task(r.writer())
        await r.log_queue.join()
        assert r.log_errors == 2
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
