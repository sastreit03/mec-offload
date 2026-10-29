from pathlib import Path
import hashlib
import math
import yaml
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


Application = Literal["detection", "tracking", "segmentation"]


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    application: Application
    model: str
    artifact: str
    input_hw: tuple[int, int]
    precision: Literal["fp16"]
    bitrate_bps: int = Field(gt=0)
    confidence: float = Field(default=0.1, ge=0, le=1)
    class_map_id: str
    output_schema: str
    backend: Literal["torchscript"] = "torchscript"


# Reject unknown catalog fields rather than silently accepting misspelled settings.
class TaskCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    codec: Literal["H264"] = "H264"
    source_hw: tuple[int, int] = (720, 1280)
    fps: int = Field(default=30, gt=0)
    gop_frames: int = Field(default=30, gt=0)
    configurations: list[Candidate]


class KeyframeInterleaving(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    mode: str = "experimental"
    catalog: str
    profiles: str
    listen: str = "0.0.0.0"
    advertised_ip: str = "192.168.72.136"
    control_port: int = 8765
    first_udp_port: int = 5000
    max_videos: int = 30
    pending_frames: int = 16
    pending_bytes_per_video: int = 64 * 1024 * 1024
    decoded_max_bytes: int = 8 * 1024 * 1024
    encoded_max_bytes: int = 8 * 1024 * 1024
    jitter_ms: int = 20
    udp_buffer_bytes: int = 2097152
    gop_frames: int = 30
    fps: int = 30
    slo_s: float = 0.15
    first_media_s: float = 5.0
    # Give prerecorded clients time to prepare the next receiver before EOF.
    successor_prepare_s: float = 5.0
    final_arrival_s: float = 0.5
    drain_s: float = 5.0
    disconnect_s: float = 5.0
    receive_loss_s: float = 1.0
    snapshot_s: float = 5.0
    tracker_buffer: int = 8
    tracker_retention_s: float = 1.0
    tracker_margin_s: float = 0.01
    applications: tuple[Application, ...] = (
        "detection",
        "tracking",
        "segmentation",
    )
    priority_streams: tuple[int, ...] = (-5, -3, 0)
    keyframe_interleaving: KeyframeInterleaving = Field(
        default_factory=KeyframeInterleaving
    )
    edgeric_target: str = "192.168.71.140:50052"
    awareness_timeout_s: float = 0.25
    gop_lead_s: float = 1.0
    runs_dir: str = "runs"
    weights_dir: str = "weights"
    backend: str = "torchscript"
    warmup_count: int | None = None
    radio_budget: float | None = None
    compute_budget_s_per_s: float | None = None
    global_queue_initial_s: float | None = None
    profile_compatibility: str | None = None
    allow_scheduled_throughput_estimate: bool = False
    max_clock_uncertainty_ns: int = 20_000_000
    gop_policy: str = "heuristic"
    priority_policy: str = "heuristic"
    # Collection runs select one immutable catalog candidate per application.
    fixed_candidates: dict[Application, str] = Field(default_factory=dict)
    ran_telemetry: dict = Field(
        default_factory=lambda: dict(
            aggregation_window_ms=100,
            report_interval_ms=100,
            stale_ms=300,
            prolonged_outage_s=5,
        )
    )

    # Experimental runs require explicit calibration and cannot use mock execution.
    def check(self):
        if self.mode not in ("experimental", "development", "collection"):
            raise ValueError("mode")
        if self.backend not in ("torchscript", "mock"):
            raise ValueError("backend")
        if self.mode in ("experimental", "collection") and self.backend == "mock":
            raise ValueError("mock forbidden in experimental mode")
        if self.mode != "collection" and (
            self.gop_policy != "heuristic" or self.priority_policy != "heuristic"
        ):
            raise ValueError("RL policy implementation deferred")
        if self.mode == "collection":
            if self.priority_streams != (-3,):
                raise ValueError("collection mode requires priority stream -3")
            if set(self.fixed_candidates) != set(self.applications):
                raise ValueError(
                    "collection mode requires one fixed candidate per application"
                )
        for key in ("warmup_count", "profile_compatibility"):
            if getattr(self, key) is None:
                raise ValueError(f"required profiled setting: {key}")
        if self.mode != "collection":
            for key in (
                "radio_budget",
                "compute_budget_s_per_s",
                "global_queue_initial_s",
            ):
                if getattr(self, key) is None:
                    raise ValueError(f"required profiled setting: {key}")
        if self.fps <= 0 or self.gop_frames <= 0:
            raise ValueError("positive FPS and GoP frame length required")
        if self.successor_prepare_s <= 0:
            raise ValueError("positive successor preparation time required")
        if not self.applications or len(set(self.applications)) != len(
            self.applications
        ):
            raise ValueError("applications must be nonempty and unique")
        if not 1 <= len(self.priority_streams) <= 3:
            raise ValueError("one to three priority streams supported by the heuristic")
        if tuple(sorted(set(self.priority_streams))) != self.priority_streams:
            raise ValueError("priority streams must be unique and ordered high-to-low")
        if self.warmup_count < 1:
            raise ValueError("positive profiled warmup count required")
        if self.mode != "collection":
            if (
                not all(
                    math.isfinite(x)
                    for x in (
                        self.radio_budget,
                        self.compute_budget_s_per_s,
                        self.global_queue_initial_s,
                    )
                )
                or self.global_queue_initial_s < 0
            ):
                raise ValueError("finite profile settings required")
            if self.radio_budget <= 0 or self.compute_budget_s_per_s <= 0:
                raise ValueError("positive budgets")


def load(path):
    path = Path(path).resolve()
    s = Settings(**yaml.safe_load(path.read_text()))
    s.check()

    def resolve(name):
        p = Path(name)
        return p if p.is_absolute() else path.parent / p

    c = TaskCatalog(**yaml.safe_load(resolve(s.catalog).read_text())).model_dump(
        mode="json"
    )
    profiles = yaml.safe_load(resolve(s.profiles).read_text())
    ids = [x["id"] for x in c["configurations"]]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate catalog configuration")
    if c["source_hw"] != [720, 1280]:
        raise ValueError("initial profiled workload requires 1280x720 source video")
    if c["fps"] != s.fps or c["gop_frames"] != s.gop_frames:
        raise ValueError("catalog FPS/GoP must match MEC settings")
    if s.mode != "collection":
        for key in (
            "mean_s",
            "P0_s2",
            "R_s2",
            "q_s2_per_s",
            "V_frame_s2",
            "radio_bps_per_unit",
        ):
            value = profiles.get("network", {}).get(key)
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"missing finite positive calibration: network.{key}")
    if profiles.get("compatibility") != s.profile_compatibility:
        raise ValueError("incompatible profile set")
    if profiles.get("synthetic") and s.mode in ("experimental", "collection"):
        raise ValueError("synthetic profiles forbidden")
    if s.mode == "collection":
        by_id = {candidate["id"]: candidate for candidate in c["configurations"]}
        measured = collection_candidates(c, profiles)
        for application, candidate_id in s.fixed_candidates.items():
            candidate = by_id.get(candidate_id)
            if candidate is None or candidate["application"] != application:
                raise ValueError(f"invalid fixed candidate for {application}")
            if candidate_id not in measured:
                raise ValueError(
                    f"fixed candidate lacks measured execution profile: {candidate_id}"
                )
    return s, c, profiles


def collection_candidates(catalog, profiles):
    """Return measured execution candidates without applying quality qualification."""
    result = {}
    for candidate in catalog["configurations"]:
        candidate = dict(candidate)
        profile = profiles.get("candidates", {}).get(candidate["id"])
        required = ("processing_s", "frame_bytes_I", "frame_bytes_P")
        if not profile or any(
            not isinstance(profile.get(key), (int, float))
            or not math.isfinite(profile[key])
            or profile[key] <= 0
            for key in required
        ):
            continue
        if (
            candidate["application"] == "tracking"
            and not profile.get("tracker_s", 0) > 0
        ):
            continue
        candidate.update(profile)
        result[candidate["id"]] = candidate
    return result


# Only candidates with quality evidence and complete timing initialization survive.
def qualified(catalog, profiles):
    result = {}
    for c in catalog["configurations"]:
        c = dict(c)
        p = profiles.get("candidates", {}).get(c["id"])
        if p and any(
            not isinstance(p.get(k), (int, float))
            or not math.isfinite(p[k])
            or p[k] <= 0
            for k in ("score", "reference_score")
        ):
            continue
        # Preliminary experiments retain every candidate with measured quality.
        if not p or not p.get("qualified"):
            continue
        required = ("processing_s", "residence_s", "frame_bytes_I", "frame_bytes_P")
        if any(
            not isinstance(p.get(k), (int, float))
            or not math.isfinite(p[k])
            or p[k] <= 0
            for k in required
        ):
            continue
        if c["application"] == "tracking" and not p.get("tracker_s", 0) > 0:
            continue
        c.update(p)
        result[c["id"]] = c
    return result
