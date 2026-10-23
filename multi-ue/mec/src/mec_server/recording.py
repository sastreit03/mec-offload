"""M31–M34. Disk IO and durable replay are outside receiver/control/GPU callbacks."""

from pathlib import Path
import json
import os
import queue
import threading
import time
import copy


class Recorder:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue = queue.Queue(maxsize=16384)
        self.error = None
        self.thread = threading.Thread(target=self._writer, daemon=True)
        self.thread.start()

    def put(self, filename, record):
        # Refuse silent evidence loss. Health/readiness exposes failure.
        try:
            self.queue.put_nowait((filename, copy.deepcopy(record)))
        except queue.Full:
            self.error = "recording_queue_overflow"

    def event(self, kind, **fields):
        self.put(
            "events.jsonl",
            dict(
                type=kind,
                monotonic_ns=time.monotonic_ns(),
                unix_ns=time.time_ns(),
                **fields,
            ),
        )

    def snapshot(self, value):
        self.put("run.json", value)

    # Only this background thread writes snapshots and append-only experiment logs.
    def _writer(self):
        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            name, value = item
            try:
                path = self.root / name
                if name == "run.json":
                    temp = path.with_suffix(".tmp")
                    temp.write_text(json.dumps(value, allow_nan=False, indent=2))
                    os.replace(temp, path)
                else:
                    with path.open("a") as f:
                        f.write(
                            json.dumps(value, allow_nan=False, separators=(",", ":"))
                            + "\n"
                        )
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.queue.task_done()

    def close(self):
        self.queue.put(None)
        self.thread.join(timeout=10)


class ReplayStore:
    """SQLite-backed full payload retention; accessed with asyncio.to_thread by ControlHub."""

    def __init__(self, path):
        import sqlite3

        self.path = str(path)
        self.write_lock = threading.Lock()
        with sqlite3.connect(self.path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                "CREATE TABLE IF NOT EXISTS results (ue TEXT,video TEXT,frame INTEGER,result TEXT PRIMARY KEY,payload TEXT)"
            )

    def save(self, ue, video, frame, result, payload):
        import sqlite3

        serialized = json.dumps(payload, allow_nan=False)
        with self.write_lock, sqlite3.connect(self.path, timeout=30) as c:
            c.execute(
                "INSERT OR IGNORE INTO results VALUES (?,?,?,?,?)",
                (ue, video, frame, result, serialized),
            )

    def pages(self, ue, after=0, limit=32):
        import sqlite3

        with sqlite3.connect(self.path) as c:
            return [
                (rowid, json.loads(payload))
                for rowid, payload in c.execute(
                    "SELECT rowid,payload FROM results WHERE ue=? AND rowid>? ORDER BY rowid LIMIT ?",
                    (ue, after, limit),
                )
            ]

    def maximum(self, ue):
        import sqlite3

        with sqlite3.connect(self.path) as c:
            return c.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM results WHERE ue=?", (ue,)
            ).fetchone()[0]

    # A fixed rowid cutoff keeps reconnect replay finite while live results arrive.
    def ordered_page(self, ue, cutoff, offset, limit):
        import sqlite3

        with sqlite3.connect(self.path) as c:
            return [
                json.loads(row[0])
                for row in c.execute(
                    "SELECT payload FROM results WHERE ue=? AND rowid<=? ORDER BY video,frame LIMIT ? OFFSET ?",
                    (ue, cutoff, limit, offset),
                )
            ]
