from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class VideoConfig(BaseModel):
    source: str = "/opt/ue-demo/video/demo.mp4"
    fps: int = Field(default=5, ge=1, le=30)
    width: int = Field(default=640, ge=160, le=3840)
    height: int = Field(default=360, ge=120, le=2160)
    bitrate_kbps: int = Field(default=400, ge=50, le=20_000)
    gop_frames: int = Field(default=10, ge=1, le=300)
    crop_left: int = Field(default=0, ge=0)
    crop_right: int = Field(default=0, ge=0)
    crop_top: int = Field(default=0, ge=0)
    crop_bottom: int = Field(default=0, ge=0)
    preview_jpeg_quality: int = Field(default=80, ge=30, le=95)
    rtp_mtu: int = Field(default=1200, ge=576, le=1400)
    loop_video: bool = False

    @field_validator("width", "height")
    @classmethod
    def must_be_even(cls, value: int) -> int:
        # I420 / H.264 is simplest and most portable with even dimensions.
        if value % 2:
            raise ValueError("must be even")
        return value


class NetworkConfig(BaseModel):
    mec_ip: str
    video_port: int = Field(default=5000, ge=1, le=65535)
    control_ws_url: str
    expected_ue_interface: str | None = "oaitun_ue1"
    strict_route_check: bool = True


class DashboardConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class AppConfig(BaseModel):
    video: VideoConfig
    network: NetworkConfig
    dashboard: DashboardConfig = DashboardConfig()

    @model_validator(mode="after")
    def source_must_be_nonempty(self) -> "AppConfig":
        if not self.video.source.strip():
            raise ValueError("video.source must not be empty")
        return self


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AppConfig.model_validate(data)


class VideoUpdate(BaseModel):
    fps: int | None = Field(default=None, ge=1, le=30)
    width: int | None = Field(default=None, ge=160, le=3840)
    height: int | None = Field(default=None, ge=120, le=2160)
    bitrate_kbps: int | None = Field(default=None, ge=50, le=20_000)
    gop_frames: int | None = Field(default=None, ge=1, le=300)
    crop_left: int | None = Field(default=None, ge=0)
    crop_right: int | None = Field(default=None, ge=0)
    crop_top: int | None = Field(default=None, ge=0)
    crop_bottom: int | None = Field(default=None, ge=0)
    preview_jpeg_quality: int | None = Field(default=None, ge=30, le=95)
    rtp_mtu: int | None = Field(default=None, ge=576, le=1400)

    @field_validator("width", "height")
    @classmethod
    def optional_even(cls, value: int | None) -> int | None:
        if value is not None and value % 2:
            raise ValueError("must be even")
        return value


class QuestionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SharedState:
    """Thread-safe state shared by GStreamer threads, FastAPI, and WebSocket client."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        self.latest_jpeg: bytes | None = None
        self.preview_sequence = 0
        self.preview_pts_ns: int | None = None

        self.stream_running = False
        self.stream_error: str | None = None
        self.stream_started_unix_ns: int | None = None

        self.rtp_packets_total = 0
        self.rtp_bytes_total = 0
        self.rtp_bitrate_kbps = 0.0
        self.rtp_packets_per_s = 0.0
        self._rate_window_started = time.monotonic()
        self._rate_window_bytes = 0
        self._rate_window_packets = 0

        self.control_connected = False
        self.control_last_error: str | None = None
        self.question_counter = 0
        self.active_question: dict[str, Any] | None = None
        self.latest_result: dict[str, Any] | None = None
        self.last_control_message: dict[str, Any] | None = None

        self.route_description: str | None = None

    def set_preview(self, jpeg: bytes, pts_ns: int | None) -> None:
        with self._lock:
            self.latest_jpeg = jpeg
            self.preview_pts_ns = pts_ns
            self.preview_sequence += 1

    def get_preview(self) -> tuple[bytes | None, int, int | None]:
        with self._lock:
            return self.latest_jpeg, self.preview_sequence, self.preview_pts_ns

    def set_stream_running(self, running: bool, error: str | None = None) -> None:
        with self._lock:
            self.stream_running = running
            self.stream_error = error
            if running:
                self.stream_started_unix_ns = time.time_ns()

    def set_stream_error(self, error: str) -> None:
        with self._lock:
            self.stream_error = error

    def record_rtp_packet(self, byte_count: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.rtp_packets_total += 1
            self.rtp_bytes_total += byte_count
            self._rate_window_packets += 1
            self._rate_window_bytes += byte_count
            dt = now - self._rate_window_started
            if dt >= 1.0:
                self.rtp_bitrate_kbps = self._rate_window_bytes * 8.0 / dt / 1000.0
                self.rtp_packets_per_s = self._rate_window_packets / dt
                self._rate_window_started = now
                self._rate_window_bytes = 0
                self._rate_window_packets = 0

    def set_route(self, description: str) -> None:
        with self._lock:
            self.route_description = description

    def new_question(self, text: str) -> dict[str, Any]:
        with self._lock:
            self.question_counter += 1
            q = {
                "type": "question",
                "question_id": self.question_counter,
                "text": text.strip(),
                "sent_unix_ns": time.time_ns(),
            }
            self.active_question = q
            # Clear the visible answer immediately so an old answer isn't mistaken for
            # the answer to a newly updated question.
            self.latest_result = None
            return dict(q)

    def set_control_connected(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self.control_connected = connected
            self.control_last_error = error

    def set_control_message(self, message: dict[str, Any]) -> None:
        with self._lock:
            self.last_control_message = message
            if message.get("type") == "vlm_result":
                active_id = (
                    self.active_question.get("question_id")
                    if self.active_question is not None
                    else None
                )
                # Only show the result as current if it belongs to the current question.
                if active_id is None or message.get("question_id") == active_id:
                    self.latest_result = message

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stream": {
                    "running": self.stream_running,
                    "error": self.stream_error,
                    "started_unix_ns": self.stream_started_unix_ns,
                    "preview_sequence": self.preview_sequence,
                    "preview_pts_ns": self.preview_pts_ns,
                    "rtp_packets_total": self.rtp_packets_total,
                    "rtp_bytes_total": self.rtp_bytes_total,
                    "rtp_bitrate_kbps": round(self.rtp_bitrate_kbps, 1),
                    "rtp_packets_per_s": round(self.rtp_packets_per_s, 1),
                },
                "control": {
                    "connected": self.control_connected,
                    "last_error": self.control_last_error,
                    "active_question": self.active_question,
                    "latest_result": self.latest_result,
                    "last_message": self.last_control_message,
                },
                "route": self.route_description,
            }
