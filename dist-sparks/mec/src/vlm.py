from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import numpy as np
from PIL import Image

from models import DecodedFrame, SharedState, VLMConfig


LOG = logging.getLogger("mec.vlm")


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _extract_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    stripped = _JSON_FENCE_RE.sub("", stripped).strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed, None
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidate = stripped[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, None
        except json.JSONDecodeError as exc:
            return None, str(exc)
    return None, "No JSON object found in model output"


class VLMEngine:
    """Lazy-loaded Qwen3-VL inference engine.

    Frames are supplied as an ordered multi-image sequence. This avoids writing a
    temporary video file while still giving the VLM chronological visual context.
    """

    def __init__(self, config: VLMConfig, state: SharedState) -> None:
        self._cfg = config.model_copy(deep=True)
        self._state = state
        self._model: Any = None
        self._processor: Any = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._state.snapshot()["vlm"]["status"] == "ready"

    def load(self) -> None:
        if self._cfg.backend == "mock":
            self._state.set_model_status("ready", model_id="mock")
            LOG.warning("VLM backend is MOCK. No real model inference will run.")
            return

        self._state.set_model_status("loading", model_id=self._cfg.model_id)
        try:
            import torch
            from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

            LOG.info("Loading VLM %s", self._cfg.model_id)
            kwargs: dict[str, Any] = {
                "dtype": "auto",
                "device_map": "auto",
            }
            if self._cfg.attn_implementation:
                kwargs["attn_implementation"] = self._cfg.attn_implementation

            model = Qwen3VLForConditionalGeneration.from_pretrained(
                self._cfg.model_id,
                **kwargs,
            )
            processor = AutoProcessor.from_pretrained(self._cfg.model_id)
            model.eval()

            self._model = model
            self._processor = processor
            self._state.set_model_status("ready", model_id=self._cfg.model_id)
            LOG.info("VLM ready: %s", self._cfg.model_id)
        except Exception as exc:  # noqa: BLE001
            self._state.set_model_status(
                "error", model_id=self._cfg.model_id, error=str(exc)
            )
            LOG.exception("Failed to load VLM: %s", exc)

    def infer(
        self,
        *,
        frames: list[DecodedFrame],
        question: dict[str, Any],
    ) -> dict[str, Any]:
        if not frames:
            raise ValueError("No frames supplied")

        with self._lock:
            if self._cfg.backend == "mock":
                time.sleep(0.05)
                return {
                    "event_detected": False,
                    "answer": "Mock backend: media/control pipeline is working; real VLM is disabled.",
                    "confidence": None,
                    "details": "Set vlm.backend to transformers for Qwen3-VL inference.",
                    "raw_model_output": None,
                }

            if self._model is None or self._processor is None:
                raise RuntimeError("VLM is not loaded")

            return self._infer_transformers(frames=frames, question=question)

    def _infer_transformers(
        self,
        *,
        frames: list[DecodedFrame],
        question: dict[str, Any],
    ) -> dict[str, Any]:
        import torch

        pil_frames = [self._prepare_image(frame.rgb) for frame in frames]
        t0 = frames[0].arrival_monotonic_ns
        rel_times = [
            round((frame.arrival_monotonic_ns - t0) / 1e9, 3) for frame in frames
        ]
        span_s = rel_times[-1] if rel_times else 0.0

        question_text = str(question.get("text", "")).strip()
        prompt = self._build_prompt(
            question_text=question_text,
            frame_count=len(frames),
            rel_times=rel_times,
            span_s=span_s,
        )

        content: list[dict[str, Any]] = [
            {"type": "image", "image": image} for image in pil_frames
        ]
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]

        inputs = self._processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        inputs.pop("token_type_ids", None)
        inputs = inputs.to(self._model.device)

        generation_kwargs: dict[str, Any] = {
            "max_new_tokens": self._cfg.max_new_tokens,
            "do_sample": self._cfg.temperature > 0.0,
        }
        if self._cfg.temperature > 0.0:
            generation_kwargs["temperature"] = self._cfg.temperature

        with torch.inference_mode():
            generated = self._model.generate(**inputs, **generation_kwargs)

        input_len = inputs["input_ids"].shape[-1]
        generated_trimmed = generated[:, input_len:]
        text = self._processor.batch_decode(
            generated_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0].strip()

        parsed, parse_error = _extract_json_object(text)
        if parsed is None:
            return {
                "event_detected": None,
                "answer": text,
                "confidence": None,
                "details": None,
                "parse_error": parse_error,
                "raw_model_output": text,
            }

        event_detected = parsed.get("event_detected")
        if event_detected not in {True, False, None}:
            event_detected = None

        confidence = parsed.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence = max(0.0, min(1.0, float(confidence)))
        else:
            confidence = None

        return {
            "event_detected": event_detected,
            "answer": str(parsed.get("answer", "")).strip(),
            "confidence": confidence,
            "details": parsed.get("details"),
            "parse_error": None,
            "raw_model_output": text,
        }

    def _prepare_image(self, rgb: np.ndarray) -> Image.Image:
        image = Image.fromarray(rgb, mode="RGB")
        target = (self._cfg.frame_width, self._cfg.frame_height)
        if image.size != target:
            image = image.resize(target, Image.Resampling.BILINEAR)
        return image

    @staticmethod
    def _build_prompt(
        *,
        question_text: str,
        frame_count: int,
        rel_times: list[float],
        span_s: float,
    ) -> str:
        times = ", ".join(f"{t:.2f}" for t in rel_times)
        return f"""
You are a video-event understanding system running at a 5G MEC server.

You are given {frame_count} images sampled from one video stream. They are in strict chronological order from oldest to newest and cover approximately {span_s:.2f} seconds. Their relative times in seconds are: [{times}].

User question / monitoring task:
{question_text}

Reason across the sequence, not just the last image. For actions such as taking, placing, opening, removing, entering, or leaving, require temporal evidence of a state transition. If the sampled frames are insufficient to establish the action, say so rather than guessing.

Return ONLY one valid JSON object with exactly these fields:
{{
  "event_detected": true | false | null,
  "answer": "short direct answer",
  "confidence": 0.0,
  "details": "one short sentence describing the temporal visual evidence"
}}

Use event_detected=true/false when the question asks whether a condition/event happened. Use null for a general descriptive question. Confidence is your self-assessed confidence between 0 and 1; it is not a calibrated probability.
""".strip()
