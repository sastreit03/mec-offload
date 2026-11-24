"""M31–M34. Disk IO and durable replay are outside receiver/control/GPU callbacks."""

from pathlib import Path
import json
import os
import queue
import threading
import time
import copy
from concurrent.futures import Future
from .results import SerializedResult


class Recorder:
    def __init__(self, directory, context=None):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue = queue.Queue(maxsize=16384)
        self.error = None
        self.context = context or (lambda: {})
        self.thread = threading.Thread(target=self._writer, daemon=True)
        self.thread.start()

    def put(self, filename, record):
        # Refuse silent evidence loss. Health/readiness exposes failure.
        try:
            value = copy.deepcopy(record)
            # Aetheredge: apply join identity at enqueue time so later scenario
            # changes cannot relabel records waiting in the writer queue.
            for key, item in self.context().items():
                value.setdefault(key, item)
            self.queue.put_nowait((filename, value))
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
    """One bounded writer for all UEs; readers use separate SQLite connections."""

    def __init__(self, path, max_pending_bytes=64 * 1024 * 1024):
        import sqlite3

        self.path = str(path)
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.pending_bytes = 0
        self.max_pending_bytes = max_pending_bytes
        self.error = None
        self.closed = False
        self.tail = None
        with sqlite3.connect(self.path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                "CREATE TABLE IF NOT EXISTS results (ue TEXT,video TEXT,frame INTEGER,result TEXT PRIMARY KEY,payload TEXT)"
            )
        self.thread = threading.Thread(target=self._writer, name="mec-replay-writer", daemon=True)
        self.thread.start()

    def submit(self, ue, video, frame, result, serialized):
        """Reserve bytes and return a future completed only after the commit."""
        # Our JSON encoder emits ASCII (ensure_ascii=True), so chars equal bytes.
        size = len(serialized)
        future = Future()
        future.timing = {"replay_submitted_ns": time.monotonic_ns()}
        with self.lock:
            if self.closed:
                raise RuntimeError("replay writer closed")
            if self.error:
                raise RuntimeError("replay writer failed") from self.error
            if self.pending_bytes + size > self.max_pending_bytes:
                self.error = RuntimeError("replay pending byte limit exceeded")
                raise self.error
            self.pending_bytes += size
            self.tail = future
            self.queue.put_nowait(((ue, video, frame, result, serialized), size, future))
        return future

    def _write(self, connection, values):
        with connection:
            connection.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?,?)", values)

    def _writer(self):
        import sqlite3
        connection = None
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    return
                values, size, future = item
                started = time.monotonic_ns()
                future.timing["replay_writer_start_ns"] = started
                future.timing["replay_queue_s"] = (started-future.timing["replay_submitted_ns"])/1e9
                error = None
                try:
                    if self.error:
                        raise RuntimeError("replay writer failed") from self.error
                    if connection is None:
                        connection = sqlite3.connect(self.path, timeout=30)
                    self._write(connection, values)
                except Exception as exc:
                    with self.lock:
                        self.error = exc
                    error = exc
                finally:
                    with self.lock:
                        self.pending_bytes -= size
                if error is None:
                    ended = time.monotonic_ns()
                    future.timing.update(replay_commit_end_ns=ended, replay_write_s=(ended-started)/1e9)
                    future.set_result(None)
                else:
                    future.set_exception(error)
        finally:
            if connection is not None:
                connection.close()

    def barrier(self):
        """Snapshot existing writes; later submissions do not extend this barrier."""
        with self.lock:
            return self.tail

    def check(self):
        with self.lock:
            if self.error:
                raise RuntimeError("replay writer failed") from self.error

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.queue.put_nowait(None)
        self.thread.join()
        self.check()

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
                SerializedResult.from_storage(row[0])
                for row in c.execute(
                    "SELECT payload FROM results WHERE ue=? AND rowid<=? ORDER BY video,frame LIMIT ? OFFSET ?",
                    (ue, cutoff, limit, offset),
                )
            ]
