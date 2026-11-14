"""Local completion tracking for scenario-start WebSocket writes (not UE ACKs)."""

import asyncio
import time


class SendReceipt:
    def __init__(self):
        self.done = asyncio.get_running_loop().create_future()
        self.queued_ns = time.monotonic_ns()
        self.started_ns = None
        self.finished_ns = None

    def finish(self, error=None):
        self.finished_ns = time.monotonic_ns()
        if not self.done.done():
            self.done.set_result(error)

    def evidence(self):
        return dict(
            queued_monotonic_ns=self.queued_ns,
            write_start_monotonic_ns=self.started_ns,
            write_end_monotonic_ns=self.finished_ns,
            outcome=("pending" if not self.done.done() else "withdrawn"
                     if self.done.cancelled() else self.done.result() or "sent"),
        )
