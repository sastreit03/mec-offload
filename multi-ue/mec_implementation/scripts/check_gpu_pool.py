"""On Spark: python -m scripts.check_gpu_pool --config config.yaml

Prepare two replicas of the first CUDA config; run one HIGH and one LOW job.
This validates attachment and reported priorities, not an SLO or overlap guarantee.
"""
import argparse
import time
from tempfile import TemporaryDirectory

import numpy as np
from mec.config import load
from mec.frames import Frame
from mec.records import stamp
from mec.runlog import RunLog
from mec.workers import WorkerPool


def check(path):
    cfg, catalog = load(path)
    app = next((c for c in catalog.configurations if c.device.startswith("cuda")), None)
    if app is None or cfg.pool.max_workers < 2:
        raise ValueError("Configure a CUDA app and at least two worker slots")
    cfg.pool.require_mps = True
    with TemporaryDirectory() as root:
        log = RunLog(root)
        pool = WorkerPool(cfg.pool, log)
        workers = []
        try:
            for index in range(2):
                worker = pool.prepare(app, f"test-{index}")
                if worker is None:
                    raise ValueError("Pool memory/preparation limit cannot fit two replicas")
                workers.append(worker)
                deadline = time.monotonic() + cfg.timeouts.preparation_s
                ready = False
                while not ready and time.monotonic() < deadline:
                    for current, message in pool.poll():
                        if message["type"] == "worker_failed":
                            raise RuntimeError(message["detail"])
                        if current is worker and message["type"] == "ready":
                            pool.claim(worker, f"test-{index}")
                            print("READY", message, flush=True)
                            ready = True
                    time.sleep(0.01)
                if not ready:
                    raise TimeoutError("GPU preparation exceeded configured deadline")
            for worker, priority in zip(workers, ("high", "low")):
                frame = Frame(0, np.zeros((app.height, app.width, 3), np.uint8), time.time_ns(), time.time_ns(), {"queue_enter": stamp()})
                pool.submit(worker, frame, {}, priority)
            completed = 0
            deadline = time.monotonic() + 30
            while completed < 2 and time.monotonic() < deadline:
                for worker, message in pool.poll():
                    if message["type"] in {"job_failed", "worker_failed"}:
                        raise RuntimeError(message["detail"])
                    if message["type"] == "job_done":
                        worker.in_flight = False
                        print("JOB", {k: v for k, v in message.items() if k != "payload"}, flush=True)
                        completed += 1
                time.sleep(0.01)
            if completed != 2:
                raise TimeoutError("GPU jobs did not complete")
        finally:
            for worker in list(pool.workers.values()):
                pool.remove(worker)
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    check(parser.parse_args().config)
