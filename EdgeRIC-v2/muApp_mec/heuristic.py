from math import isfinite


def weights(
    snapshots, capacities, now_ns, b_min=1.0, delta_s=0.0005, alpha=1.0, beta=1.0
):
    if b_min <= 0 or delta_s <= 0:
        raise ValueError("positive utility floors required")
    # Score the oldest outstanding frame; overdue urgency is bounded by delta_s.
    scores = {}
    detail = {}
    for uid, s in snapshots.items():
        f = s.get("oldest")
        r = capacities.get(uid)
        if (
            not s["valid"]
            or not f
            or not r
            or r <= 0
            or f["deadline_ns"] is None
            or f["remaining_bytes"] <= 0
        ):
            continue
        d = (f["deadline_ns"] - now_ns) / 1e9
        score = (
            r
            * (f["total_bytes"] / max(f["remaining_bytes"], b_min)) ** alpha
            / max(d, delta_s) ** beta
        )
        if not isfinite(score) or score <= 0:
            continue
        scores[uid] = score
        detail[uid] = dict(
            F=f["total_bytes"],
            B=f["remaining_bytes"],
            D_s=d,
            R_bytes_prb=r,
            score=score,
            frame_id=f["frame_id"],
        )
    # Normalize only usable MEC scores; omitted UEs receive no utility weight here.
    total = sum(scores.values())
    if not isfinite(total) or total <= 0:
        return {}, detail
    return {u: s / total for u, s in scores.items()}, detail
