from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class NetworkConfig(BaseModel):
    video_bind_address: str = "0.0.0.0"
    video_port: int = Field(default=5000, ge=1, le=65535)
    control_host: str = "0.0.0.0"
    control_port: int = Field(default=8765, ge=1, le=65535)
    control_path: str = "/ws/ue"


class ReceiverConfig(BaseModel):
    rtp_payload_type: int = Field(default=96, ge=0, le=127)
    jitter_latency_ms: int = Field(default=50, ge=0, le=2000)
    udp_buffer_bytes: int = Field(default=2_097_152, ge=65_536, le=67_108_864)
    preview_jpeg_quality: int = Field(default=80, ge=30, le=95)
    frame_buffer_max_frames: int = Field(default=180, ge=2, le=5000)


class VLMConfig(BaseModel):
    # Set backend to "mock" first to validate the full network/media path.
    backend: str = "transformers"
    model_id: str = "Qwen/Qwen3-VL-4B-Instruct"
    attn_implementation: str = "sdpa"
    inference_interval_s: float = Field(default=2.0, ge=0.25, le=60.0)
    window_seconds: float = Field(default=3.0, ge=0.25, le=60.0)
    max_frames: int = Field(default=10, ge=2, le=64)
    min_frames: int = Field(default=3, ge=1, le=64)
    frame_width: int = Field(default=448, ge=112, le=1536)
    frame_height: int = Field(default=252, ge=112, le=1536)
    max_new_tokens: int = Field(default=128, ge=16, le=1024)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    clear_buffer_on_new_question: bool = True
    discard_result_if_question_changed: bool = True
    include_raw_model_output: bool = False

    @field_validator("backend")
    @classmethod
    def valid_backend(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"transformers", "mock"}:
            raise ValueError("backend must be 'transformers' or 'mock'")
        return value

    @field_validator("frame_width", "frame_height")
    @classmethod
    def even_dimension(cls, value: int) -> int:
        if value % 2:
            raise ValueError("must be even")
        return value

    @model_validator(mode="after")
    def min_not_greater_than_max(self) -> "VLMConfig":
        if self.min_frames > self.max_frames:
            raise ValueError("min_frames must be <= max_frames")
        return self


class AppConfig(BaseModel):
    network: NetworkConfig = NetworkConfig()
    receiver: ReceiverConfig = ReceiverConfig()
    vlm: VLMConfig = VLMConfig()


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


@dataclass(frozen=True)
class DecodedFrame:
    sequence: int
    arrival_unix_ns: int
    arrival_monotonic_ns: int
    pts_ns: int | None
    width: int
    height: int
    rgb: np.ndarray


class FrameBuffer:
    """Thread-safe rolling buffer of fully decoded RGB frames."""

    def __init__(self, max_frames: int) -> None:
        self._lock = threading.RLock()
        self._frames: deque[DecodedFrame] = deque(maxlen=max_frames)

    def append(self, frame: DecodedFrame) -> None:
        with self._lock:
            self._frames.append(frame)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    def length(self) -> int:
        with self._lock:
            return len(self._frames)

    def newest_sequence(self) -> int | None:
        with self._lock:
            return self._frames[-1].sequence if self._frames else None

    def snapshot_window(
        self,
        *,
        window_seconds: float,
        max_frames: int,
        not_before_monotonic_ns: int | None = None,
    ) -> list[DecodedFrame]:
        """Return chronological frames from the recent time window.

        If there are more than max_frames, uniformly select across the entire
        interval so the VLM sees the beginning, middle, and end of the action.
        """
        now_ns = time.monotonic_ns()
        cutoff_ns = now_ns - int(window_seconds * 1e9)
        if not_before_monotonic_ns is not None:
            cutoff_ns = max(cutoff_ns, not_before_monotonic_ns)

        with self._lock:
            candidates = [
                f for f in self._frames if f.arrival_monotonic_ns >= cutoff_ns
            ]

        if len(candidates) <= max_frames:
            return candidates

        # Uniform subsampling, preserving first and last frame.
        indices = np.linspace(0, len(candidates) - 1, max_frames, dtype=int)
        return [candidates[int(i)] for i in indices]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            if not self._frames:
                return {
                    "frames": 0,
                    "oldest_unix_ns": None,
                    "newest_unix_ns": None,
                    "span_s": 0.0,
                }
            oldest = self._frames[0]
            newest = self._frames[-1]
            return {
                "frames": len(self._frames),
                "oldest_unix_ns": oldest.arrival_unix_ns,
                "newest_unix_ns": newest.arrival_unix_ns,
                "span_s": round(
                    (newest.arrival_monotonic_ns - oldest.arrival_monotonic_ns)
                    / 1e9,
                    3,
                ),
            }


class SharedState:
    """Thread-safe state shared by GStreamer callbacks and FastAPI tasks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        self.latest_jpeg: bytes | None = None
        self.preview_sequence = 0
        self.preview_pts_ns: int | None = None

        self.receiver_running = False
        self.receiver_error: str | None = None
        self.last_frame_unix_ns: int | None = None
        self.decoded_frames_total = 0
        self.decoded_fps = 0.0
        self._fps_window_started = time.monotonic()
        self._fps_window_frames = 0

        self.rtp_packets_total = 0
        self.rtp_bytes_total = 0
        self.rtp_packets_lost = 0
        self.rtp_duplicates_or_old = 0
        self.rtp_bitrate_kbps = 0.0
        self.rtp_packets_per_s = 0.0
        self.rtp_ssrc: int | None = None
        self.last_rtp_sequence: int | None = None
        self.last_rtp_timestamp: int | None = None
        self.last_rtp_unix_ns: int | None = None
        self._rtp_rate_started = time.monotonic()
        self._rtp_rate_bytes = 0
        self._rtp_rate_packets = 0

        self.control_connected = False
        self.control_peer: str | None = None
        self.control_last_error: str | None = None
        self.ue_hello: dict[str, Any] | None = None
        self.stream_config: dict[str, Any] | None = None
        self.active_question: dict[str, Any] | None = None
        self.question_received_monotonic_ns: int | None = None
        self.latest_result: dict[str, Any] | None = None
        self.last_control_message: dict[str, Any] | None = None

        self.model_status = "not_loaded"
        self.model_error: str | None = None
        self.model_id: str | None = None
        self.inference_running = False
        self.inference_count = 0
        self.last_inference_ms: float | None = None
        self.last_inference_frame_count = 0
        self.last_inference_unix_ns: int | None = None
        self.last_inference_error: str | None = None
        self.stale_results_discarded = 0

    def set_preview(self, jpeg: bytes, pts_ns: int | None) -> None:
        with self._lock:
            self.latest_jpeg = jpeg
            self.preview_pts_ns = pts_ns
            self.preview_sequence += 1

    def get_preview(self) -> tuple[bytes | None, int, int | None]:
        with self._lock:
            return self.latest_jpeg, self.preview_sequence, self.preview_pts_ns

    def set_receiver_running(self, running: bool, error: str | None = None) -> None:
        with self._lock:
            self.receiver_running = running
            self.receiver_error = error

    def record_decoded_frame(self) -> None:
        now = time.monotonic()
        with self._lock:
            self.decoded_frames_total += 1
            self.last_frame_unix_ns = time.time_ns()
            self._fps_window_frames += 1
            dt = now - self._fps_window_started
            if dt >= 1.0:
                self.decoded_fps = self._fps_window_frames / dt
                self._fps_window_started = now
                self._fps_window_frames = 0

    def record_rtp_packet(
        self,
        *,
        byte_count: int,
        sequence: int,
        timestamp: int,
        ssrc: int,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            if self.rtp_ssrc != ssrc:
                # A UE media restart can create a new SSRC and sequence-number base.
                self.rtp_ssrc = ssrc
                self.last_rtp_sequence = None

            if self.last_rtp_sequence is not None:
                delta = (sequence - self.last_rtp_sequence) & 0xFFFF
                if delta == 0 or delta >= 0x8000:
                    self.rtp_duplicates_or_old += 1
                else:
                    if delta > 1:
                        self.rtp_packets_lost += delta - 1
                    self.last_rtp_sequence = sequence
            else:
                self.last_rtp_sequence = sequence

            self.last_rtp_timestamp = timestamp
            self.last_rtp_unix_ns = time.time_ns()
            self.rtp_packets_total += 1
            self.rtp_bytes_total += byte_count
            self._rtp_rate_packets += 1
            self._rtp_rate_bytes += byte_count

            dt = now - self._rtp_rate_started
            if dt >= 1.0:
                self.rtp_bitrate_kbps = self._rtp_rate_bytes * 8.0 / dt / 1000.0
                self.rtp_packets_per_s = self._rtp_rate_packets / dt
                self._rtp_rate_started = now
                self._rtp_rate_packets = 0
                self._rtp_rate_bytes = 0

    def set_control_connected(
        self, connected: bool, peer: str | None = None, error: str | None = None
    ) -> None:
        with self._lock:
            self.control_connected = connected
            self.control_peer = peer if connected else None
            self.control_last_error = error

    def set_control_message(self, message: dict[str, Any]) -> None:
        with self._lock:
            self.last_control_message = message
            msg_type = message.get("type")
            if msg_type == "ue_hello":
                self.ue_hello = dict(message)
            elif msg_type == "stream_config":
                self.stream_config = dict(message)

    def set_question(self, message: dict[str, Any]) -> bool:
        """Set current question. Return True only when it actually changed."""
        with self._lock:
            old = self.active_question
            same = (
                old is not None
                and old.get("question_id") == message.get("question_id")
                and old.get("text") == message.get("text")
            )
            self.last_control_message = message
            if same:
                return False
            self.active_question = dict(message)
            self.question_received_monotonic_ns = time.monotonic_ns()
            self.latest_result = None
            return True

    def get_question(self) -> tuple[dict[str, Any] | None, int | None]:
        with self._lock:
            question = dict(self.active_question) if self.active_question else None
            return question, self.question_received_monotonic_ns

    def set_latest_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.latest_result = dict(result)

    def set_model_status(
        self, status: str, *, model_id: str | None = None, error: str | None = None
    ) -> None:
        with self._lock:
            self.model_status = status
            self.model_error = error
            if model_id is not None:
                self.model_id = model_id

    def inference_started(self, frame_count: int) -> None:
        with self._lock:
            self.inference_running = True
            self.last_inference_frame_count = frame_count
            self.last_inference_error = None

    def inference_finished(self, elapsed_ms: float, error: str | None = None) -> None:
        with self._lock:
            self.inference_running = False
            self.inference_count += 1
            self.last_inference_ms = elapsed_ms
            self.last_inference_unix_ns = time.time_ns()
            self.last_inference_error = error

    def record_stale_result(self) -> None:
        with self._lock:
            self.stale_results_discarded += 1

    def snapshot(self, frame_buffer: FrameBuffer | None = None) -> dict[str, Any]:
        with self._lock:
            rtp_expected = self.rtp_packets_total + self.rtp_packets_lost
            loss_pct = (
                100.0 * self.rtp_packets_lost / rtp_expected if rtp_expected else 0.0
            )
            data = {
                "receiver": {
                    "running": self.receiver_running,
                    "error": self.receiver_error,
                    "last_frame_unix_ns": self.last_frame_unix_ns,
                    "decoded_frames_total": self.decoded_frames_total,
                    "decoded_fps": round(self.decoded_fps, 2),
                    "preview_sequence": self.preview_sequence,
                    "preview_pts_ns": self.preview_pts_ns,
                },
                "rtp": {
                    "packets_total": self.rtp_packets_total,
                    "bytes_total": self.rtp_bytes_total,
                    "packets_lost_estimate": self.rtp_packets_lost,
                    "loss_percent_estimate": round(loss_pct, 3),
                    "duplicates_or_old": self.rtp_duplicates_or_old,
                    "bitrate_kbps": round(self.rtp_bitrate_kbps, 1),
                    "packets_per_s": round(self.rtp_packets_per_s, 1),
                    "ssrc": self.rtp_ssrc,
                    "last_sequence": self.last_rtp_sequence,
                    "last_timestamp": self.last_rtp_timestamp,
                    "last_packet_unix_ns": self.last_rtp_unix_ns,
                },
                "control": {
                    "connected": self.control_connected,
                    "peer": self.control_peer,
                    "last_error": self.control_last_error,
                    "ue_hello": self.ue_hello,
                    "stream_config": self.stream_config,
                    "active_question": self.active_question,
                    "latest_result": self.latest_result,
                    "last_message": self.last_control_message,
                },
                "vlm": {
                    "status": self.model_status,
                    "error": self.model_error,
                    "model_id": self.model_id,
                    "inference_running": self.inference_running,
                    "inference_count": self.inference_count,
                    "last_inference_ms": (
                        round(self.last_inference_ms, 1)
                        if self.last_inference_ms is not None
                        else None
                    ),
                    "last_inference_frame_count": self.last_inference_frame_count,
                    "last_inference_unix_ns": self.last_inference_unix_ns,
                    "last_inference_error": self.last_inference_error,
                    "stale_results_discarded": self.stale_results_discarded,
                },
            }
        if frame_buffer is not None:
            data["frame_buffer"] = frame_buffer.stats()
        return data
