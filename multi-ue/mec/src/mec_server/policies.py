"""D14/D55–D58: discrete adjacent probability-gradient moves; no concurrency model."""

from dataclasses import dataclass
from math import erf, sqrt, isfinite
from typing import Protocol


class GoPPolicy(Protocol):
    def propose(self, ues, candidates, radio_budget, compute_budget): ...


class PriorityPolicy(Protocol):
    def propose(self, slo_s, network_s, waiting_s, queue_s, processing_s): ...


class HeuristicPriority:
    version = "slack-v1"

    def __init__(self, streams=(-5, -3, 0)):
        self.streams = tuple(streams)

    # Waiting already spent and the queue allowance overlap, so take their maximum.
    def propose(self, slo_s, network_s, waiting_s, queue_s, processing_s):
        u = (slo_s - network_s - max(waiting_s, queue_s) - processing_s) / slo_s
        if u <= 0:
            return None
        urgency = 0 if u <= 0.2 else 1 if u <= 0.5 else 2
        # Preserve three existing slack classes while allowing one or two streams.
        index = (urgency * (len(self.streams) - 1) + 1) // 2
        return self.streams[index]


def probability(slo, bitrate, mu, variance, processing):
    beta = bitrate / 1e6
    mean = beta * mu + processing
    variance *= beta * beta
    if variance <= 0:
        return float(mean <= slo)
    return 0.5 * (1 + erf((slo - mean) / sqrt(2 * variance)))


# Compute cost is seconds of work per second, not a count of CUDA streams.
def costs(assignment, ues, candidates):
    radio, compute = 0.0, 0.0
    for uid, cid in assignment.items():
        c, u = candidates[cid], ues[uid]
        radio += c["bitrate_bps"] / u["radio_bps_per_unit"]
        compute += c["processing_s"] * u["fps"]
    return radio, compute


# Candidate membership alone is insufficient: the complete assignment must fit.
def validate_joint(assignment, ues, candidates, radio_budget, compute_budget):
    if set(assignment) != set(ues):
        return False
    if any(
        cid not in candidates or cid not in ues[uid]["candidate_ids"]
        for uid, cid in assignment.items()
    ):
        return False
    if any(
        not isfinite(u["radio_bps_per_unit"]) or u["radio_bps_per_unit"] <= 0
        for u in ues.values()
    ):
        return False
    r, c = costs(assignment, ues, candidates)
    return r <= radio_budget and c <= compute_budget


class HeuristicGoP:
    version = "probability-gradient-v1"

    def propose(self, ues, candidates, radio_budget, compute_budget):
        ordered, probs, chosen = {}, {}, {}
        for uid, u in ues.items():
            ordered[uid] = sorted(
                u["candidate_ids"],
                key=lambda cid: (candidates[cid]["processing_s"], cid),
            )
            probs[uid] = {
                cid: probability(
                    u["slo_s"],
                    candidates[cid]["bitrate_bps"],
                    u["mu_s"],
                    u["variance_s2"],
                    candidates[cid]["processing_s"],
                )
                for cid in ordered[uid]
            }
            if not ordered[uid] or u["radio_bps_per_unit"] <= 0:
                return None
            chosen[uid] = max(
                ordered[uid],
                key=lambda cid: (probs[uid][cid], -ordered[uid].index(cid)),
            )
        seen = set()
        # Radio overload moves toward lower bitrate; compute overload toward lower processing.
        # Ties and simultaneous overload use deterministic radio-first ordering, recorded in docs.
        while not validate_joint(chosen, ues, candidates, radio_budget, compute_budget):
            key = tuple(sorted(chosen.items()))
            if key in seen:
                return None  # search failure, not proof of infeasibility
            seen.add(key)
            r, c = costs(chosen, ues, candidates)
            moves = []
            for uid, cid in chosen.items():
                index = ordered[uid].index(cid)
                for j in [index - 1, index + 1]:
                    if not 0 <= j < len(ordered[uid]):
                        continue
                    nxt = ordered[uid][j]
                    a, b = candidates[cid], candidates[nxt]
                    helps = (
                        b["bitrate_bps"] < a["bitrate_bps"]
                        if r > radio_budget
                        else b["processing_s"] < a["processing_s"]
                    )
                    if helps:
                        moves.append((abs(probs[uid][cid] - probs[uid][nxt]), uid, nxt))
            if not moves:
                return None
            _, uid, nxt = min(moves)
            chosen[uid] = nxt
        return chosen
