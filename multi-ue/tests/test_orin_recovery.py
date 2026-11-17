import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("recover_orin", Path(__file__).parents[1] / "scripts/recover_orin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_recovery_preserves_arguments_and_waits_for_tunnel_before_client(tmp_path, monkeypatch):
    launch = dict(argv=["/repo/scripts/softue-client-supervisor.sh", "--source-dir", "/data/run 2",
                        "--logs", "/logs/run 2"], cwd="/repo", env={"SOFTUE_CONTAINER": "oai-nr-ue"})
    monkeypatch.setattr(module, "STATE", tmp_path / "launch.json")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "find_launch", lambda: (None, launch))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    calls = []
    def run(argv, **kwargs):
        assert kwargs["check"]
        calls.append(argv)
        return SimpleNamespace(stdout="true\n")
    def popen(argv, **kwargs):
        calls.append(argv)
        assert kwargs["cwd"] == launch["cwd"]
        assert kwargs["env"]["SOFTUE_CONTAINER"] == "oai-nr-ue"
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    module.main([])
    assert [c[1] for c in calls[:4]] == ["inspect", "exec", "stop", "start"]
    assert calls[4] == [str(tmp_path / "scripts/softue-route.sh")]
    assert calls[5] == launch["argv"]


def test_split_recovery_stays_stopped_until_explicit_start(tmp_path, monkeypatch):
    launch = dict(argv=["/repo/supervisor", "--logs", "/logs/run 2"], cwd="/repo", env={})
    monkeypatch.setattr(module, "STATE", tmp_path / "launch.json")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "find_launch", lambda: (None, launch))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="true\n")
    def popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    module.main(["--phase", "stop"])
    assert [c[1] for c in calls] == ["inspect", "exec", "stop"]
    assert json.loads(module.STATE.read_text()) == launch
    calls.clear()
    module.main(["--phase", "start"])
    assert calls == [["docker", "start", "oai-nr-ue"],
                     [str(tmp_path / "scripts/softue-route.sh")], launch["argv"]]


@pytest.mark.parametrize("failure", ["", "stop", "recreate", "health"])
def test_campaign_quiets_orin_before_gnb_restart(tmp_path, failure):
    repo = tmp_path / "repo"
    (repo / "gnb-core/config/common").mkdir(parents=True)
    scripts = repo / "multi-ue/scripts"
    scripts.mkdir(parents=True)
    bins = tmp_path / "bin"
    bins.mkdir()
    trace = tmp_path / "trace"
    def executable(path, body):
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
    executable(bins / "ssh", '''
echo "ssh $*" >> "$TRACE"
[[ "$FAILURE" == stop && "$*" == *"--phase stop"* ]] && exit 1
exit 0
''')
    executable(bins / "docker", '''
echo "docker $*" >> "$TRACE"
if [[ "$*" == *"--force-recreate"* ]]; then
  [[ "$FAILURE" == recreate ]] && exit 1
elif [[ "$*" == *"State.Health.Status"* ]]; then
  if [[ "$FAILURE" == health ]]; then echo unhealthy; else echo healthy; fi
elif [[ "$*" == *"State.Running"* ]]; then echo true
fi
exit 0
''')
    executable(bins / "timeout", 'shift\nexec "$@"\n')
    executable(bins / "sleep", 'exit 0\n')
    executable(bins / "curl", 'exit 0\n')
    executable(bins / "seq", 'echo 1\n')
    executable(scripts / "containers.sh", 'echo "containers $*" >> "$TRACE"\n')
    script = Path(__file__).parents[1] / "scripts/campaign_recover.sh"
    result = subprocess.run(["bash", str(script), "gnb_telemetry_invalid", "scenario", "attempt"],
                            env=dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}",
                                     AETHEREDGE_REPO_ROOT=str(repo),
                                     AETHEREDGE_CAMPAIGN_DIR=str(tmp_path / "campaign"),
                                     TRACE=str(trace), FAILURE=failure), capture_output=True, text=True)
    lines = trace.read_text().splitlines()
    stop = next(i for i, line in enumerate(lines) if "--phase stop" in line)
    recreate = [i for i, line in enumerate(lines) if "--force-recreate" in line]
    start = [i for i, line in enumerate(lines) if "--phase start" in line]
    if failure:
        assert result.returncode != 0
        assert not start
        if failure == "stop":
            assert not recreate
    else:
        assert result.returncode == 0, result.stderr
        assert stop < recreate[0] < start[0]
        assert any("State.Health.Status" in line for line in lines[recreate[0]+1:start[0]])
