"""Adapter contract: prepare(), warmup(), infer(rgb, parameters), reset(), close().

Add another importable adapter without editing admission or worker-pool code.
Model ownership stays in a spawned worker. Return JSON-compatible task payloads.
"""
import time
import numpy as np


class MockAdapter:
    def __init__(self, config):
        self.config = config

    def prepare(self):
        time.sleep(self.config.options.get("prepare_delay_s", 0))
        if self.config.options.get("fail_prepare", False):
            raise RuntimeError("Requested mock preparation failure")

    def warmup(self):
        self.infer(np.zeros((self.config.height, self.config.width, 3), np.uint8), {})

    def infer(self, rgb, parameters):
        time.sleep(self.config.options.get("infer_delay_s", 0.01))
        if parameters.get("fail_inference"):
            raise RuntimeError("Requested mock inference failure")
        return {"schema_version": 1, "model": self.config.model,
                "mean_rgb": rgb.mean(axis=(0, 1)).tolist()}

    def reset(self):
        pass

    def close(self):
        pass


class YOLOAdapter:
    """Detection, classification and tracking with an already-local checkpoint.

Segmentation result representation and multi-model detection+recognition are
left to their own adapters, as agreed; no fake implementation of those tasks.
"""
    def __init__(self, config):
        self.config = config

    def prepare(self):
        from pathlib import Path
        from ultralytics import YOLO
        if not Path(self.config.model).is_file():
            raise ValueError("Download model artifacts before launch; worker startup never downloads")
        self.model = YOLO(self.config.model)
        self.model.to(self.config.device)

    def warmup(self):
        self.infer(np.zeros((self.config.height, self.config.width, 3), np.uint8), {})
        self.reset()

    def infer(self, rgb, parameters):
        options = {"device": self.config.device, "imgsz": (self.config.height, self.config.width),
                   "verbose": False, "conf": self.config.options.get("confidence", 0.25)}
        # Ultralytics numpy inputs are BGR; our transport/queue is consistently RGB.
        bgr = rgb[:, :, ::-1].copy()
        if self.config.options.get("mode") == "track":
            result = self.model.track(bgr, persist=True, **options)[0]
        else:
            result = self.model.predict(bgr, **options)[0]
        payload = {"schema_version": 1, "model": self.config.model, "names": result.names}
        if result.boxes is not None:
            boxes = result.boxes
            payload["detections"] = [{"xyxy": xy, "confidence": conf, "class_id": int(cls), "track_id": track}
                                     for xy, conf, cls, track in zip(
                                         boxes.xyxy.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.cls.cpu().tolist(),
                                         boxes.id.cpu().tolist() if boxes.id is not None else [None] * len(boxes))]
        if result.probs is not None:
            payload["classification"] = {"class_ids": result.probs.top5, "probabilities": result.probs.top5conf.cpu().tolist()}
        return payload

    def reset(self):
        predictor = getattr(self.model, "predictor", None)
        for tracker in getattr(predictor, "trackers", []):
            tracker.reset()

    def close(self):
        del self.model
