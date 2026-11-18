from pathlib import Path
import yaml
from muApp_mec.runtime import Runtime


def test_status_cadence_expiry_and_console_output(tmp_path, capsys):
    config = yaml.safe_load((Path(__file__).parents[1] / 'config/edgeric.shadow.yaml').read_text())
    config['runs_dir'] = str(tmp_path)
    r = Runtime(config)
    r.console_logs = False
    b = r.associations.register('u', 'c', '12.1.1.3')
    b.rnti, b.valid_until = 100, 3
    r.association_console_status(0)
    r.association_console_status(1.99)
    assert r.log_queue.qsize() == 1
    r._append(r.log_queue.get_nowait())
    assert 'C-RNTI=100 (0x0064) IP=12.1.1.3' in capsys.readouterr().out
    r.association_aliases["u"] = "pixel1"
    r.association_console_status(2)
    event = r.log_queue.get_nowait()
    assert event['bindings'][0]['alias'] == 'pixel1'
    r._append(event)
    assert 'alias=pixel1' in capsys.readouterr().out
    r.association_console_status(4)
    r._append(r.log_queue.get_nowait())
    assert 'associations (0): none' in capsys.readouterr().out
    r.associations.removed('u', 'absent_from_sync')
    r._append(r.log_queue.get_nowait())
    output = capsys.readouterr().out
    assert 'UNASSOCIATED: C-RNTI=100 (0x0064) IP=12.1.1.3' in output
