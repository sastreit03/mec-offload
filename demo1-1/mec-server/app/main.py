"""GPU-backed YOLO inference API for the OAI MEC.

The module is intended to be started by Uvicorn as:

    uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1

It implements the endpoints and safeguards described in the implementation
manual: process liveness, model/GPU readiness, bounded image validation,
single-load model warm-up, bounded inference concurrency, normalized boxes,
and per-request server timing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status, Request
from ultralytics import YOLO


LOGGER = logging.getLogger("mec-yolo")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer environment variable with a useful error."""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer; received {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}; received {value}")
    return value


def _env_float(name: str, default: float) -> float:
    """Read a floating-point environment variable with a useful error."""
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric; received {raw!r}") from exc


MODEL_PATH = Path(os.getenv("MODEL_PATH", "/models/yolo11n.pt"))
DEVICE = os.getenv("DEVICE", "0").strip()
DEFAULT_IMGSZ = _env_int("DEFAULT_IMGSZ", 640)
DEFAULT_CONF = _env_float("DEFAULT_CONF", 0.25)
DEFAULT_IOU = _env_float("DEFAULT_IOU", 0.70)
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_BYTES", 8 * 1024 * 1024)
# The manual requires a decoded-pixel bound but does not prescribe its value.
# This default permits, for example, a 6000 x 6000 image while preventing an
# encoded "decompression bomb" from consuming unbounded memory.
MAX_IMAGE_PIXELS = _env_int("MAX_IMAGE_PIXELS", 36_000_000)
MAX_CONCURRENT_INFERENCE = _env_int("MAX_CONCURRENT_INFERENCE", 1)
MIN_IMGSZ = _env_int("MIN_IMGSZ", 160)
MAX_IMGSZ = _env_int("MAX_IMGSZ", 1920)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}

_model: YOLO | None = None
_ready = False
_startup_time_s: float | None = None
_inference_slots = asyncio.Semaphore(MAX_CONCURRENT_INFERENCE)


def _uses_cuda(device: str) -> bool:
    """Return True when the configured Ultralytics device requests CUDA."""
    return device.lower() not in {"cpu", "mps"}


def _detect_encoded_type(data: bytes) -> str | None:
    """Identify JPEG, PNG, or WebP from file signatures."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_runtime_configuration() -> None:
    """Validate settings before the model is loaded."""
    if not 0.0 < DEFAULT_CONF <= 1.0:
        raise RuntimeError("DEFAULT_CONF must be in the range (0, 1]")
    if not 0.0 < DEFAULT_IOU <= 1.0:
        raise RuntimeError("DEFAULT_IOU must be in the range (0, 1]")
    if MIN_IMGSZ > MAX_IMGSZ:
        raise RuntimeError("MIN_IMGSZ cannot exceed MAX_IMGSZ")
    if not MIN_IMGSZ <= DEFAULT_IMGSZ <= MAX_IMGSZ:
        raise RuntimeError(
            f"DEFAULT_IMGSZ must be between {MIN_IMGSZ} and {MAX_IMGSZ}"
        )
    if not MODEL_PATH.is_file():
        raise RuntimeError(f"YOLO checkpoint does not exist: {MODEL_PATH}")
    if _uses_cuda(DEVICE) and not torch.cuda.is_available():
        raise RuntimeError(
            f"DEVICE={DEVICE!r} requests GPU inference, but CUDA is unavailable"
        )


def _load_and_warm_model() -> YOLO:
    """Load the checkpoint exactly once and execute one warm-up inference."""
    LOGGER.info("Loading YOLO checkpoint %s on device %s", MODEL_PATH, DEVICE)
    loaded_model = YOLO(str(MODEL_PATH))

    # A synthetic image warms model initialization and CUDA kernels without
    # retaining or requiring a user-provided image.
    warm_image = np.zeros((DEFAULT_IMGSZ, DEFAULT_IMGSZ, 3), dtype=np.uint8)
    with torch.inference_mode():
        loaded_model.predict(
            source=warm_image,
            conf=DEFAULT_CONF,
            iou=DEFAULT_IOU,
            imgsz=DEFAULT_IMGSZ,
            device=DEVICE,
            verbose=False,
        )
    if _uses_cuda(DEVICE):
        torch.cuda.synchronize()
    return loaded_model


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize CUDA/model state before readiness is advertised."""
    global _model, _ready, _startup_time_s

    started = time.perf_counter()
    _validate_runtime_configuration()
    _model = await asyncio.to_thread(_load_and_warm_model)
    _startup_time_s = time.perf_counter() - started
    _ready = True

    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    LOGGER.info(
        "MEC YOLO service ready model=%s device=%s gpu=%s warmup_s=%.3f",
        MODEL_PATH.name,
        DEVICE,
        gpu_name,
        _startup_time_s,
    )

    try:
        yield
    finally:
        _ready = False
        _model = None
        LOGGER.info("MEC YOLO service stopped")

# HTTP transfer timing
class RequestReceiveTimingMiddleware:
    """Measure how long an HTTP request body takes to arrive through ASGI."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_ns = time.perf_counter_ns()
        body_bytes = 0

        async def timed_receive() -> dict[str, Any]:
            nonlocal body_bytes

            message = await receive()

            if message["type"] == "http.request":
                body = message.get("body", b"")

                if body:
                    body_bytes += len(body)

                if not message.get("more_body", False):
                    state = scope.setdefault("state", {})

                    receive_ns = time.perf_counter_ns() - start_ns

                    # Record time of end of HTTP post transmission
                    state["post_complete_time_ns"] = time.time_ns()

                    state["request_receive_ms"] = receive_ns / 1_000_000.0
                    state["request_body_bytes"] = body_bytes

                    if receive_ns > 0:
                        state["request_receive_mbps"] = (
                            body_bytes * 8 * 1000 / receive_ns
                        )
                    else:
                        state["request_receive_mbps"] = None

            return message

        # custom send function to get start time of HTTP response
        async def timed_send(message):
            if message["type"] == "http.response.start":
                start_ns = time.time_ns()

                headers = list(message.get("headers", []))
                headers.append(
                    (b"x-response-start-time-ns", str(start_ns).encode())
                )
                message["headers"] = headers

            await send(message)

        await self.app(scope, timed_receive, timed_send)


app = FastAPI(
    title="OAI MEC YOLO",
    version="1.0",
    lifespan=lifespan,
)


app.add_middleware(RequestReceiveTimingMiddleware)

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Process-liveness endpoint: Uvicorn can execute this handler."""
    return {"status": "alive"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    """Readiness endpoint: model load and warm-up must have completed."""
    if not _ready or _model is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is not ready",
        )

    return {
        "status": "ready",
        "model": MODEL_PATH.name,
        "device": DEVICE,
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "max_concurrent_inference": MAX_CONCURRENT_INFERENCE,
        "startup_warmup_ms": round((_startup_time_s or 0.0) * 1000.0, 3),
    }


def _validated_task_id(task_id: str | None) -> str:
    """Apply the Appendix A task-ID contract."""
    if task_id is None or not task_id.strip():
        return str(uuid.uuid4())

    value = task_id.strip()
    if not 1 <= len(value) <= 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task_id must contain 1 to 128 characters",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task_id cannot contain control characters",
        )
    return value


def _validate_inference_parameters(conf: float, iou: float, imgsz: int) -> None:
    """Validate the confidence, IoU, and inference-size form fields."""
    if not 0.0 < conf <= 1.0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="conf must be in the range (0, 1]",
        )
    if not 0.0 < iou <= 1.0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="iou must be in the range (0, 1]",
        )
    if not MIN_IMGSZ <= imgsz <= MAX_IMGSZ:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"imgsz must be between {MIN_IMGSZ} and {MAX_IMGSZ}",
        )


def _predict(
    decoded_image: np.ndarray,
    *,
    conf: float,
    iou: float,
    imgsz: int,
) -> tuple[Any, float]:
    """Run one synchronous Ultralytics inference call in a worker thread."""
    if _model is None:
        raise RuntimeError("Model disappeared after readiness")

    if _uses_cuda(DEVICE):
        torch.cuda.synchronize()
    started = time.perf_counter()
    with torch.inference_mode():
        results = _model.predict(
            source=decoded_image,
            conf=conf,
            iou=iou,
            imgsz=imgsz,
            device=DEVICE,
            verbose=False,
        )
    if _uses_cuda(DEVICE):
        torch.cuda.synchronize()
    inference_ms = (time.perf_counter() - started) * 1000.0

    if not results:
        raise RuntimeError("Ultralytics returned no result object")
    return results[0], inference_ms


def _serialize_detections(result: Any, width: int, height: int) -> list[dict[str, Any]]:
    """Convert Ultralytics boxes into normalized, JSON-safe metadata."""
    detections: list[dict[str, Any]] = []
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return detections

    xyxy = boxes.xyxy.detach().cpu().numpy()
    confidences = boxes.conf.detach().cpu().numpy()
    class_ids = boxes.cls.detach().cpu().numpy().astype(int)
    names = getattr(result, "names", None) or getattr(_model, "names", {})

    for coordinates, confidence, class_id in zip(xyxy, confidences, class_ids):
        x1, y1, x2, y2 = coordinates.tolist()
        normalized = [
            float(np.clip(x1 / width, 0.0, 1.0)),
            float(np.clip(y1 / height, 0.0, 1.0)),
            float(np.clip(x2 / width, 0.0, 1.0)),
            float(np.clip(y2 / height, 0.0, 1.0)),
        ]
        if isinstance(names, dict):
            class_name = str(names.get(class_id, class_id))
        else:
            class_name = str(names[class_id]) if class_id < len(names) else str(class_id)

        detections.append(
            {
                "class_id": int(class_id),
                "class_name": class_name,
                "confidence": round(float(confidence), 6),
                "bbox_xyxy_norm": [round(value, 6) for value in normalized],
            }
        )
    return detections


@app.post("/v1/detect")
async def detect(
    request: Request,
    image: UploadFile = File(...),
    task_id: str | None = Form(default=None),
    conf: float = Form(default=DEFAULT_CONF),
    iou: float = Form(default=DEFAULT_IOU),
    imgsz: int = Form(default=DEFAULT_IMGSZ),
) -> dict[str, Any]:
    """Decode one uploaded image, run YOLO, and return metadata only."""
    request_started = time.perf_counter()

    # Get transfer timing from middleware
    request_receive_ms = getattr(
        request.state,
        "request_receive_ms",
        None,
    )

    request_body_bytes = getattr(
        request.state,
        "request_body_bytes",
        None,
    )

    request_receive_mbps = getattr(
        request.state,
        "request_receive_mbps",
        None,
    )

    # Get HTTP post end time from request state
    post_complete_time_ns = getattr(
        request.state,
        "post_complete_time_ns",
        None
    )

    if not _ready or _model is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model is not ready",
        )

    resolved_task_id = _validated_task_id(task_id)
    _validate_inference_parameters(conf, iou, imgsz)

    try:
        declared_type = (image.content_type or "").lower()
        canonical_declared_type = ALLOWED_CONTENT_TYPES.get(declared_type)
        if canonical_declared_type is None:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Only image/jpeg, image/png, and image/webp are accepted",
            )

        # Read one byte beyond the limit so an oversized request is detected
        # without loading an unbounded file into application memory.
        encoded = await image.read(MAX_IMAGE_BYTES + 1)
        if not encoded:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded image is empty",
            )
        if len(encoded) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Encoded image exceeds {MAX_IMAGE_BYTES} bytes",
            )

        detected_type = _detect_encoded_type(encoded)
        if detected_type is None:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Image bytes are not a supported JPEG, PNG, or WebP file",
            )
        if detected_type != canonical_declared_type:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=(
                    f"Declared content type {declared_type!r} does not match "
                    f"the encoded file type {detected_type!r}"
                ),
            )

        decode_started = time.perf_counter()
        encoded_array = np.frombuffer(encoded, dtype=np.uint8)
        decoded = cv2.imdecode(encoded_array, cv2.IMREAD_COLOR)
        decode_ms = (time.perf_counter() - decode_started) * 1000.0

        if decoded is None or decoded.size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Image could not be decoded",
            )

        height, width = decoded.shape[:2]
        decoded_pixels = int(width) * int(height)
        if decoded_pixels > MAX_IMAGE_PIXELS:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Decoded image contains {decoded_pixels} pixels; "
                    f"limit is {MAX_IMAGE_PIXELS}"
                ),
            )

        queue_started = time.perf_counter()
        async with _inference_slots:
            queue_wait_ms = (time.perf_counter() - queue_started) * 1000.0
            result, inference_call_ms = await asyncio.to_thread(
                _predict,
                decoded,
                conf=conf,
                iou=iou,
                imgsz=imgsz,
            )

        postprocess_started = time.perf_counter()
        detections = _serialize_detections(result, width, height)
        ultralytics_speed = getattr(result, "speed", {}) or {}

        # Build the response first, then record how long response preparation took.
        response: dict[str, Any] = {
            "task_id": resolved_task_id,
            "model": MODEL_PATH.name,
            "device": DEVICE,
            "input": {
                "filename": Path(image.filename or "upload").name,
                "content_type": detected_type,
                "bytes": len(encoded),
                "width": width,
                "height": height,
                "imgsz": imgsz,
            },
            "transfer": {
                "http_request_body_bytes": request_body_bytes,
                "server_receive_ms": (
                    round(request_receive_ms, 3)
                    if request_receive_ms is not None
                    else None
                ),
                "server_receive_mbps": (
                    round(request_receive_mbps, 3)
                    if request_receive_mbps is not None
                    else None
                ),
                "post_complete_time_ns": (
                    post_complete_time_ns
                    if post_complete_time_ns is not None
                    else None
                ),
            },
            "detections": detections,
            "timing_ms": {
                "decode": round(decode_ms, 3),
                "queue_wait": round(queue_wait_ms, 3),
                "inference_call": round(inference_call_ms, 3),
                # Filled immediately below after response construction.
                "postprocess_response": 0.0,
                "server_total": 0.0,
                "ultralytics": {
                    "preprocess": round(float(ultralytics_speed.get("preprocess", 0.0)), 3),
                    "inference": round(float(ultralytics_speed.get("inference", 0.0)), 3),
                    "postprocess": round(float(ultralytics_speed.get("postprocess", 0.0)), 3),
                },
            },
        }

        postprocess_ms = (time.perf_counter() - postprocess_started) * 1000.0
        server_total_ms = (time.perf_counter() - request_started) * 1000.0
        response["timing_ms"]["postprocess_response"] = round(postprocess_ms, 3)
        response["timing_ms"]["server_total"] = round(server_total_ms, 3)

        LOGGER.info(
            "task_id=%s bytes=%d image=%dx%d detections=%d "
            "decode_ms=%.3f queue_ms=%.3f inference_ms=%.3f server_ms=%.3f",
            resolved_task_id,
            len(encoded),
            width,
            height,
            len(detections),
            decode_ms,
            queue_wait_ms,
            inference_call_ms,
            server_total_ms,
        )
        return response

    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("task_id=%s inference request failed", resolved_task_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Inference/runtime failure; inspect MEC server logs",
        ) from exc
    finally:
        await image.close()
