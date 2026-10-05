"""Serializable rows. Live sockets, model objects and secrets live elsewhere."""
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import Field
from .config import Schema


def stamp() -> dict[str, int]:
    return {"unix_ns": time.time_ns(), "mono_ns": time.monotonic_ns()}


class SLO(Schema):
    latency_ms: float = Field(gt=0)
    latency_origin: Literal["capture", "transmission"] = "capture"
    accuracy: dict[str, Any] | None = None


class OffloadRequest(Schema):
    type: str = "offload_request"
    ue_id: str
    client_seq: int = Field(ge=0)
    ip_address: str
    task_type: str
    input_type: str = "video"
    slo: SLO
    can_drop: bool = False
    can_drop_frames: bool = False
    ue_request_send_unix_ns: int | None = None
    # Application arguments (e.g. a question) are interpreted only by the adapter.
    parameters: dict[str, Any] = {}


@dataclass
class UE:
    ue_id: str
    peer_ip: str
    reported_ip: str | None = None
    connection_id: str | None = None
    connection_state: str = "connected"
    activity: str = "idle"
    first_seen: dict = field(default_factory=stamp)
    last_seen: dict = field(default_factory=stamp)
    disconnected_mono_ns: int | None = None
    task_ids: list[str] = field(default_factory=list)
    active_task_ids: list[str] = field(default_factory=list)


@dataclass
class Task:
    task_id: str
    ue_id: str
    client_seq: int
    request: dict
    status: str = "awaiting_decision"
    times: dict = field(default_factory=dict)
    accepted: bool = False
    config: dict | None = None
    priority: str = "low"
    decision_source: str | None = None
    reason: str | None = None
    endpoint: dict | None = None
    worker_id: str | None = None
    response: dict | None = None
    counts: dict = field(default_factory=lambda: {"completed": 0, "dropped": 0, "failed": 0, "lost": 0})
    total_queue_ms: float = 0
    total_processing_ms: float = 0
    result_ref: str = "jobs.jsonl"
    terminal: bool = False

    def row(self):
        return asdict(self)
