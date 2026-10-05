"""Validate deployment settings and the task catalog before opening sockets."""
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(Schema):
    config_id: str
    task_type: str
    input_types: list[Literal["video", "image"]] = ["video", "image"]
    # An adapter is an importable Class, not a special case in the controller.
    adapter: str = "mec.adapters:MockAdapter"
    model: str = "mock"
    codec: Literal["jpeg", "h264"] = "jpeg"
    width: int = Field(default=320, gt=0)
    height: int = Field(default=240, gt=0)
    fps: int = Field(default=15, gt=0)
    bitrate_kbps: int = Field(default=1000, gt=0)
    device: str = "cpu"
    warmup_iterations: int = Field(default=2, ge=1)
    estimated_gpu_gib: float | None = Field(default=None, gt=0)
    accuracy_profile: dict[str, Any] | None = None
    options: dict[str, Any] = {}


class Catalog(Schema):
    version: str = "1"
    configurations: list[AppConfig]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [c.config_id for c in self.configurations]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("Catalog requires nonempty, unique configuration IDs")
        return self

    def get(self, config_id: str) -> AppConfig:
        return next(c for c in self.configurations if c.config_id == config_id)


class Network(Schema):
    bind_host: str = "0.0.0.0"
    advertise_host: str = "127.0.0.1"
    control_port: int = Field(default=8765, ge=0, le=65535)
    control_path: str = "/ws/ue"
    media_bind_host: str = "0.0.0.0"
    media_port_first: int = Field(default=5000, gt=0, le=65535)
    media_port_last: int = Field(default=5100, gt=0, le=65535)


class PoolSettings(Schema):
    # These are explicit example deployment values, not algorithm assumptions.
    max_workers: int = Field(default=2, gt=0)
    max_concurrent_preparations: int = Field(default=1, gt=0)
    memory_budget_gib: float | None = Field(default=None, gt=0)
    initial_replicas: dict[str, int] = {}
    idle_retention_s: float = Field(default=300, gt=0)
    shutdown_grace_s: float = Field(default=5, gt=0)
    require_mps: bool = False


class Queues(Schema):
    max_pending_frames: int = Field(default=8, gt=0)
    max_waiting_tasks: int = Field(default=5, gt=0)
    decoded_buffer_budget_mib: float | None = Field(default=None, gt=0)
    frame_policy: str = "mec.frames:FIFO"


class Timeouts(Schema):
    worker_wait_s: float = Field(default=10, gt=0)
    preparation_s: float = Field(default=15, gt=0)
    execution_ready_s: float = Field(default=25, gt=0)
    first_media_s: float = Field(default=5, gt=0)
    final_frame_arrival_grace_s: float = Field(default=0.5, ge=0)
    drain_s: float = Field(default=5, gt=0)
    disconnect_grace_s: float = Field(default=5, gt=0)
    decision_s: float = Field(default=1, gt=0)


class MediaSettings(Schema):
    jitter_latency_ms: int = Field(default=20, ge=0)
    socket_receive_bytes: int = Field(default=2097152, gt=0)
    max_frame_bytes: int = Field(default=8388608, gt=0)
    max_assembly_bytes: int = Field(default=16777216, gt=0)
    max_partial_frames: int = Field(default=32, gt=0)
    assembly_timeout_s: float = Field(default=0.25, gt=0)
    appsrc_max_buffers: int = Field(default=256, gt=0)
    mtu: int = Field(default=1200, ge=256, le=1400)


class RICSettings(Schema):
    decision_url: str | None = None
    identity_url: str | None = None
    telemetry_target: str | None = None
    reporting_period_s: float = Field(default=1, gt=0)
    period_origin_unix_ns: int = 0
    reconnect_s: float = Field(default=2, gt=0)


class Settings(Schema):
    catalog_path: str = "tasks.yaml"
    network: Network = Network()
    pool: PoolSettings = PoolSettings()
    queues: Queues = Queues()
    timeouts: Timeouts = Timeouts()
    media: MediaSettings = MediaSettings()
    ric: RICSettings = RICSettings()
    logs_dir: str = "runs"
    snapshot_interval_s: float = Field(default=5, gt=0)
    recent_jobs: int = Field(default=256, gt=0)
    tick_s: float = Field(default=0.02, gt=0)


def load(path: str) -> tuple[Settings, Catalog]:
    path = Path(path).resolve()
    cfg = Settings.model_validate(yaml.safe_load(path.read_text()))
    cat = Catalog.model_validate(yaml.safe_load((path.parent / cfg.catalog_path).read_text()))
    if cfg.network.media_port_last < cfg.network.media_port_first:
        raise ValueError("Invalid media port range")
    if cfg.network.advertise_host in {"0.0.0.0", "::"}:
        raise ValueError("advertise_host must be reachable by UEs")
    if cfg.pool.max_concurrent_preparations > cfg.pool.max_workers:
        raise ValueError("Preparation limit cannot exceed worker limit")
    for cid, count in cfg.pool.initial_replicas.items():
        cat.get(cid)
        if count < 0:
            raise ValueError("Replica counts must be nonnegative")
    if sum(cfg.pool.initial_replicas.values()) > cfg.pool.max_workers:
        raise ValueError("Initial replicas exceed pool capacity")
    if cfg.pool.memory_budget_gib is not None:
        for app in cat.configurations:
            if app.device.startswith("cuda") and app.estimated_gpu_gib is None:
                raise ValueError("GPU memory budget requires estimates for every CUDA configuration")
            if app.estimated_gpu_gib and app.estimated_gpu_gib > cfg.pool.memory_budget_gib:
                raise ValueError("A configuration's GPU reservation exceeds the pool memory budget")
    return cfg, cat
