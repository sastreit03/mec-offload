from __future__ import annotations
import json
import math
import time
import uuid


def now_ns():
    return time.monotonic_ns()


def envelope(kind, run_id, **fields):
    return dict(schema_version=1, type=kind, run_id=run_id, **fields)


def dumps(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def loads(raw):
    def invalid(value):
        raise ValueError(f"nonfinite JSON: {value}")

    value = json.loads(raw, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def finite(value, minimum=None):
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
    ):
        raise ValueError("finite number required")
    if minimum is not None and value < minimum:
        raise ValueError("number below minimum")
    return value


def integer(value, minimum=0, maximum=2**64 - 1):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError("integer out of range")
    return value


def result_id(video_id, frame_id, outcome):
    return str(uuid.uuid5(uuid.UUID(video_id), f"{frame_id}/{outcome}"))


def policy_features(value):
    """Recursive allowlist boundary; unknown keys are never implicitly policy features."""
    allowed = {
        "ue_id",
        "application",
        "slo_s",
        "candidate_ids",
        "current_config",
        "network_mean_s",
        "network_variance_s2",
        "queues",
        "streams",
        "age_s",
        "valid",
        "frame_id",
        "waiting_s",
        "deadline_remaining_s",
        "model",
        "processing_s",
        "fps",
        "budget",
        "history",
        "active",
    }
    if isinstance(value, dict):
        return {k: policy_features(v) for k, v in value.items() if k in allowed}
    if isinstance(value, list):
        return [policy_features(v) for v in value]
    return value
