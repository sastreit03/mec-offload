from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from control import ControlHub
from models import FrameBuffer, SharedState, VLMConfig
from vlm import VLMEngine


LOG = logging.getLogger("mec.scheduler")


class InferenceScheduler:
    def __init__(
        self,
        config: VLMConfig,
        state: SharedState,
        frame_buffer: FrameBuffer,
        engine: VLMEngine,
    ) -> None:
        self._cfg = config.model_copy(deep=True)
        self._state = state
        self._frames = frame_buffer
        self._engine = engine
        self._control: ControlHub | None = None
        self._wake = asyncio.Event()
        self._answered_question_id: int | None = None

    def attach_control(self, control: ControlHub) -> None:
        self._control = control

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._cfg.inference_interval_s
                )
            except TimeoutError:
                pass
            self._wake.clear()

            try:
                await self._maybe_infer()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Inference scheduler iteration failed: %s", exc)

    async def _maybe_infer(self) -> None:
        if self._control is None:
            return
        if not self._engine.ready:
            return

        question, question_since_ns = self._state.get_question()
        if question is None:
            return

        question_id = question.get("question_id")
        if question_id == self._answered_question_id:
            return

        frames = self._frames.snapshot_window(
            window_seconds=self._cfg.window_seconds,
            max_frames=self._cfg.max_frames,
            not_before_monotonic_ns=question_since_ns,
        )
        if len(frames) < self._cfg.min_frames:
            return

        #newest_seq = frames[-1].sequence
        #if newest_seq == self._last_inferred_frame_sequence:
        #    return
        #self._last_inferred_frame_sequence = newest_seq

        question_id = question.get("question_id")
        self._state.inference_started(len(frames))
        started = time.perf_counter()
        try:
            model_result = await asyncio.to_thread(
                self._engine.infer,
                frames=frames,
                question=question,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._state.inference_finished(elapsed_ms)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._state.inference_finished(elapsed_ms, error=str(exc))
            LOG.exception("VLM inference failed: %s", exc)
            result = {
                "type": "vlm_result",
                "question_id": question_id,
                "question": question.get("text"),
                "event_detected": None,
                "answer": "VLM inference failed.",
                "confidence": None,
                "error": str(exc),
                "frame_count": len(frames),
                "inference_ms": round(elapsed_ms, 1),
                "sent_unix_ns": time.time_ns(),
            }
            await self._control.send(result)
            self._state.set_latest_result(result)
            return

        current_question, _ = self._state.get_question()
        if (
            self._cfg.discard_result_if_question_changed
            and current_question is not None
            and current_question.get("question_id") != question_id
        ):
            self._state.record_stale_result()
            LOG.info(
                "Discarding result for stale question #%s; current is #%s",
                question_id,
                current_question.get("question_id"),
            )
            return

        result: dict[str, Any] = {
            "type": "vlm_result",
            "question_id": question_id,
            "question": question.get("text"),
            "window_start_unix_ns": frames[0].arrival_unix_ns,
            "window_end_unix_ns": frames[-1].arrival_unix_ns,
            "window_span_s": round(
                (frames[-1].arrival_monotonic_ns - frames[0].arrival_monotonic_ns)
                / 1e9,
                3,
            ),
            "first_frame_sequence": frames[0].sequence,
            "last_frame_sequence": frames[-1].sequence,
            "frame_count": len(frames),
            "event_detected": model_result.get("event_detected"),
            "answer": model_result.get("answer"),
            "confidence": model_result.get("confidence"),
            "details": model_result.get("details"),
            "model": self._state.snapshot()["vlm"]["model_id"],
            "inference_ms": round(elapsed_ms, 1),
            "sent_unix_ns": time.time_ns(),
        }
        if model_result.get("parse_error"):
            result["parse_error"] = model_result.get("parse_error")
        if self._cfg.include_raw_model_output:
            result["raw_model_output"] = model_result.get("raw_model_output")

        self._state.set_latest_result(result)
        await self._control.send(result)
        self._answered_question_id = question_id

        LOG.info(
            "VLM result q=%s frames=%d inference=%.1fms event=%s answer=%s",
            question_id,
            len(frames),
            elapsed_ms,
            result.get("event_detected"),
            result.get("answer"),
        )
