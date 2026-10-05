"""One background disk writer, periodic atomic snapshots, no raw model outputs."""
import json
import os
import queue
import threading
from pathlib import Path
from uuid import uuid4

from .records import stamp


class RunLog:
    def __init__(self, root: str):
        self.run_id = str(uuid4())
        self.path = Path(root) / self.run_id
        self.path.mkdir(parents=True)
        self.pending = queue.Queue()
        self.error = None
        self.thread = threading.Thread(target=self._write, name="run-log", daemon=True)
        self.thread.start()

    def event(self, name: str, **fields):
        self.pending.put(("events", {"run_id": self.run_id, "event": name, "time": stamp(), **fields}))

    def job(self, record: dict):
        # Persist timing and compact metadata. The full payload remains in the
        # delivery history, not in experiment files (e.g. segmentation masks).
        self.pending.put(("jobs", {"run_id": self.run_id, **record}))

    def snapshot(self, data: dict):
        self.pending.put(("snapshot", {"schema_version": 1, "run_id": self.run_id, **data}))

    def _write(self):
        try:
            with (self.path / "events.jsonl").open("a") as events, (self.path / "jobs.jsonl").open("a") as jobs:
                files = {"events": events, "jobs": jobs}
                while True:
                    kind, value = self.pending.get()
                    if kind == "stop":
                        break
                    if kind == "snapshot":
                        tmp = self.path / "run.json.tmp"
                        with tmp.open("w") as out:
                            json.dump(value, out, indent=2, allow_nan=False)
                            out.flush()
                            os.fsync(out.fileno())
                        tmp.replace(self.path / "run.json")
                    else:
                        files[kind].write(json.dumps(value, allow_nan=False) + "\n")
                        # Line flush is deliberate: crash exposure is only the
                        # writer's queued records / OS cache. Snapshots use fsync.
                        files[kind].flush()
        except Exception as exc:
            self.error = repr(exc)

    def close(self):
        self.pending.put(("stop", None))
        self.thread.join(timeout=10)
        if self.error or self.thread.is_alive():
            raise RuntimeError(f"Run logging did not finish: {self.error or 'writer timeout'}")
