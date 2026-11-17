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


def print_start_timing(attempt_id, begin_ns, queue_ns, elapsed_ns, sends, successful):
    """Print one batch after release checks; these are local writes, not UE ACKs."""
    def ms(value):
        return "n/a" if value is None else f"{value / 1e6:.2f}ms"

    starts = [s["write_start_monotonic_ns"] for attempts in sends.values()
              for s in attempts if s["write_start_monotonic_ns"] is not None]
    all_started = all(any(s["write_start_monotonic_ns"] is not None for s in attempts)
                      for attempts in sends.values())
    spread = max(starts) - min(starts) if starts and all_started else None
    retries = sum(max(0, len(attempts) - 1) for attempts in sends.values())
    lines = [
        f"START attempt={attempt_id} ues={len(sends)} queue={ms(queue_ns)} "
        f"write_start_spread={ms(spread)} total={ms(elapsed_ns)} "
        f"retries={retries} status={'OK' if successful else 'FAILED'}",
        f"{'UE':<12} {'Send':>4} {'Write began*':>14} {'Write duration':>16}  Result",
    ]
    for alias, attempts in sends.items():
        for number, s in enumerate(attempts, 1):
            start, end = s["write_start_monotonic_ns"], s["write_end_monotonic_ns"]
            offset = None if start is None else start - begin_ns
            duration = None if start is None or end is None else end - start
            lines.append(f"{alias:<12} {number:>4} {ms(offset):>14} {ms(duration):>16}  {s['outcome']}")
        if not attempts:
            lines.append(f"{alias:<12} {'-':>4} {'n/a':>14} {'n/a':>16}  not queued")
    lines.append("* From MEC release beginning. Local writes only; spread includes retries. n/a = unavailable.")
    try:
        print("\n".join(lines), flush=True)
    except OSError:
        # A closed terminal must not invalidate an otherwise successful release.
        pass
