"""Serialized experimental traffic-signature association, with expiring leases."""

from dataclasses import dataclass
from math import sqrt
import secrets
import time


def correlation(a, b):
    if len(a) < 6 or len(a) != len(b):
        return -1.0
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    aa = sum((v - ma) ** 2 for v in a)
    bb = sum((v - mb) ** 2 for v in b)
    if aa <= 0 or bb <= 0:
        return -1.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / sqrt(aa * bb)


@dataclass
class Binding:
    ue_id: str
    connection_id: str
    ip: str
    rnti: int | None = None
    generation: int = 0
    revalidate_at: float = 0.0
    valid_until: float = 0.0
    connection_generation: int = 0
    association_revision: int = 0


class Associations:
    def __init__(self, config):
        self.config = config
        self.bindings = {}
        self.probe = None
        self.source_generation = None
        self.pending = []
        self.last_result = {}

    # Connection generations and address revisions fence delayed identity updates.
    def register(
        self, uid, connection, ip, connection_generation=None, association_revision=0
    ):
        old = self.bindings.get(uid)
        if (
            old
            and connection_generation is not None
            and (
                connection_generation < old.connection_generation
                or connection_generation == old.connection_generation
                and old.connection_id != connection
            )
        ):
            return old
        if old and old.connection_id == connection and old.ip == ip:
            old.association_revision = max(
                old.association_revision, association_revision
            )
            return old
        if (
            old
            and connection == old.connection_id
            and association_revision < old.association_revision
        ):
            return old
        generation = old.generation + 1 if old else 1
        self.bindings[uid] = Binding(uid, connection, ip, generation=generation)
        self.bindings[uid].connection_generation = (
            connection_generation
            if connection_generation is not None
            else (old.connection_generation + 1 if old else 1)
        )
        self.bindings[uid].association_revision = association_revision
        if uid not in self.pending:
            self.pending.append(uid)
        return self.bindings[uid]

    def invalidate(self, uid, reason):
        b = self.bindings[uid]
        b.rnti = None
        b.revalidate_at = 0
        b.valid_until = 0
        b.generation += 1
        self.last_result[uid] = {"reason": reason, "valid": False}
        if uid not in self.pending:
            self.pending.append(uid)

    def tick(self, now, rntis):
        for uid, b in self.bindings.items():
            if b.rnti is not None and (b.rnti not in rntis or now >= b.valid_until):
                self.invalidate(uid, "context_missing_or_lease_expired")
        p = self.probe
        if p and now >= p["finish"]:
            measured = {}
            for rnti, bins in p["samples"].items():
                means = [sum(x) / len(x) if x else None for x in bins]
                # Missing is not zero. Require every signature interval to be observed.
                if None not in means:
                    measured[rnti] = correlation(p["pattern"], means)
            ranked = sorted(measured.items(), key=lambda x: (-x[1], x[0]))
            uid = p["ue_id"]
            best = ranked[0] if ranked else (None, -1)
            gap = best[1] - (ranked[1][1] if len(ranked) > 1 else 0)
            conflict = any(
                b.rnti == best[0] and u != uid for u, b in self.bindings.items()
            )
            b = self.bindings[uid]
            received_bytes = sum(p["received"].values())
            receipt = received_bytes >= self.config.get("min_received_bytes", 20000)
            valid = (
                receipt
                and best[0] is not None
                and best[1] >= self.config["min_correlation"]
                and gap >= self.config["min_margin"]
                and not conflict
                and b.generation == p["generation"]
            )
            if valid:
                b.rnti = best[0]
                # Aetheredge: Start revalidation before the lease deadline and
                # retain the last verified mapping for a bounded grace period.
                # Four serialized probes can otherwise make a valid UE flicker
                # false while its renewal waits behind the other three UEs.
                b.revalidate_at = (
                    now
                    + self.config["lease_s"]
                    - self.config["revalidate_before_s"]
                )
                b.valid_until = (
                    now
                    + self.config["lease_s"]
                    + self.config.get("revalidation_grace_s", 0.0)
                )
            else:
                if uid not in self.pending:
                    self.pending.append(uid)
            if not receipt:
                reason = "probe_receipt_missing"
            elif not ranked:
                reason = "ran_samples_incomplete"
            elif best[1] < self.config["min_correlation"]:
                reason = "correlation_below_threshold"
            elif gap < self.config["min_margin"]:
                reason = "margin_below_threshold"
            elif conflict:
                reason = "rnti_conflict"
            elif b.generation != p["generation"]:
                reason = "binding_generation_changed"
            else:
                reason = "matched"
            self.last_result[uid] = {
                "valid": valid,
                "scores": measured,
                "generation": b.generation,
                "reason": reason,
                "received_bytes": received_bytes,
                "best_rnti": best[0],
                "best_correlation": best[1],
                "margin": gap,
            }
            self.probe = None
        # Revalidate before expiry. No permanent trust in an unchanged numeric RNTI.
        for uid, b in self.bindings.items():
            if (
                b.rnti is not None
                and now >= b.revalidate_at
                and uid not in self.pending
            ):
                self.pending.append(uid)

    # Serialize traffic signatures so simultaneous probes do not compete for a match.
    def next_probe(self, now):
        if self.probe or not self.pending:
            return None
        uid = self.pending.pop(0)
        b = self.bindings[uid]
        pattern = [1, 0, 1, 1, 0, 0, 1, 0, 0, 1, 0, 1]
        if secrets.randbits(1):
            pattern = pattern[::-1]
        start = now + self.config["lead_s"]
        interval = self.config["interval_s"]
        self.probe = dict(
            probe_id=secrets.token_hex(8),
            ue_id=uid,
            generation=b.generation,
            pattern=pattern,
            start=start,
            finish=start + len(pattern) * interval + self.config["settle_s"],
            samples={},
            received={},
        )
        return dict(
            ue_id=uid,
            connection_id=b.connection_id,
            probe_id=self.probe["probe_id"],
            pattern=pattern,
            start_unix_ns=int((time.time() + start - now) * 1e9),
            interval_s=interval,
            rate_bps=self.config["rate_bps"],
            generation=b.generation,
        )

    # Throughput samples identify a traffic pattern; they are not unique-byte evidence.
    def sample(self, now, metrics):
        p = self.probe
        if not p:
            return
        i = int((now - p["start"]) / self.config["interval_s"])
        if now < p["start"] or not 0 <= i < len(p["pattern"]):
            return
        for rnti, m in metrics.items():
            p["samples"].setdefault(rnti, [[] for _ in p["pattern"]])[i].append(
                float(m["rx_bytes"])
            )

    def receipt(self, uid, probe, index, size):
        p = self.probe
        if p and p["ue_id"] == uid and p["probe_id"] == probe:
            p["received"][index] = p["received"].get(index, 0) + size

    def valid(self, uid, now):
        b = self.bindings.get(uid)
        return b if b and b.rnti is not None and b.valid_until > now else None
