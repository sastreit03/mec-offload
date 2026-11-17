#!/usr/bin/env python3
"""Run on Orin: preserve the current client launch, restart softUE, then client."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "runs/softue-client-launch.json"
ENV_KEYS = ("SOFTUE_CONTAINER", "SOFTUE_INTERFACE", "MEC_SERVER", "SOFTUE_DATA_NETWORK",
            "SOFTUE_TRAFFIC_SERVER", "SOFTUE_CLIENT_LOCK", "SOFTUE_CLIENT_RETRY_S")


def find_launch():
    # Read argv as separate strings; never rebuild it as a shell command.
    matches = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            args = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
            index = next((i for i, a in enumerate(args)
                          if Path(a).name == "softue-client-supervisor.sh"), None)
            if index is None:
                continue
            cwd = str((proc / "cwd").resolve())
            script = str((Path(cwd) / args[index]).resolve())
            environment = dict(x.split("=", 1) for x in
                               (proc / "environ").read_bytes().decode().split("\0") if "=" in x)
            matches.append((int(proc.name), dict(
                argv=[script, *args[index + 1:]], cwd=cwd,
                env={k: environment[k] for k in ENV_KEYS if k in environment})))
        except (OSError, UnicodeError):
            continue
    if len(matches) > 1:
        raise RuntimeError("Multiple client supervisors found; refusing ambiguous recovery")
    if matches:
        return matches[0]
    if STATE.exists():
        return None, json.loads(STATE.read_text())
    raise RuntimeError("No client supervisor or saved launch; start the supervisor with this run's arguments first")


def main():
    pid, launch = find_launch()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(launch, indent=2))
    env = dict(os.environ, **launch.get("env", {}))
    container = env.get("SOFTUE_CONTAINER", "oai-nr-ue")
    if pid:
        os.kill(pid, signal.SIGTERM)
    # Stop only this application's Python process, before stopping OAI.
    stop_client = '''import os,signal
from pathlib import Path
for p in Path('/proc').iterdir():
 try:
  args=(p/'cmdline').read_bytes().split(b'\\0')
  if b'-m' in args and args[args.index(b'-m')+1] == b'mec_ue.client':
   os.kill(int(p.name),signal.SIGTERM)
 except (OSError,ValueError,IndexError): pass
'''
    running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                             check=True, capture_output=True, text=True).stdout.strip()
    if running == "true":
        subprocess.run(["docker", "exec", container, "python3", "-c", stop_client], check=True, timeout=10)
    if pid:
        # Bash may wait for its foreground docker-exec command before handling TERM.
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(.1)
        else:
            os.kill(pid, signal.SIGKILL)
    subprocess.run(["docker", "stop", container], check=True, timeout=45)
    subprocess.run(["docker", "start", container], check=True, timeout=45)
    # Existing route helper waits for the tunnel IP and verifies the cellular route.
    subprocess.run([str(ROOT / "scripts/softue-route.sh")], env=env, check=True, timeout=75)
    with (STATE.parent / "softue-client-supervisor.log").open("ab") as log:
        child = subprocess.Popen(launch["argv"], cwd=launch["cwd"], env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
    time.sleep(1)
    if child.poll() is not None:
        raise RuntimeError("Client supervisor failed to start; inspect its log")
    print("Orin softUE restarted; client supervisor relaunched with preserved arguments", flush=True)


if __name__ == "__main__":
    main()
