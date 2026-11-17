import importlib.util
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
    module.main()
    assert [c[1] for c in calls[:4]] == ["inspect", "exec", "stop", "start"]
    assert calls[4] == [str(tmp_path / "scripts/softue-route.sh")]
    assert calls[5] == launch["argv"]
