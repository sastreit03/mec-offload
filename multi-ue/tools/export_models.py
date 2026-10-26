#!/usr/bin/env python3
"""Export locally supplied checkpoints; no implicit checkpoint download. GPU required."""

import argparse
from pathlib import Path
import json
import hashlib


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoints", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("weights"))
    a = p.parse_args()
    import torch
    from ultralytics import YOLO
    from transformers import SegformerForSemanticSegmentation

    if not torch.cuda.is_available():
        raise RuntimeError("export FP16 on target CUDA backend")

    class Segmentation(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, x):
            return self.model(pixel_values=x).logits

    # Export separate application artifacts even when detection and tracking share a family.
    records = []
    for app in ["detection", "tracking", "segmentation"]:
        names = (
            ["yolo26n", "yolo26m", "yolo26l", "yolo26x"]
            if app != "segmentation"
            else [f"segformer-b{i}" for i in range(4)]
        )
        for name in names:
            destination = a.output / app / f"{name}.torchscript"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if app == "segmentation":
                source = a.checkpoints / name
                model = (
                    Segmentation(
                        SegformerForSemanticSegmentation.from_pretrained(
                            str(source), local_files_only=True
                        )
                    )
                    .cuda()
                    .half()
                    .eval()
                )
                sample = torch.zeros(1, 3, 512, 896, device="cuda", dtype=torch.float16)
                with torch.inference_mode():
                    export = torch.jit.freeze(
                        torch.jit.trace(model, sample, strict=False)
                    )
                    export.save(str(destination))
            else:
                source = a.checkpoints / f"{name}.pt"
                if not source.is_file():
                    raise FileNotFoundError(source)
                exported = Path(
                    YOLO(str(source)).export(
                        format="torchscript",
                        imgsz=640,
                        half=True,
                        device=0,
                        batch=1,
                        nms=False,
                    )
                )
                export = torch.jit.freeze(
                    torch.jit.load(str(exported), map_location="cuda").eval()
                )
                export.save(str(destination))
                with torch.inference_mode():
                    out = export(
                        torch.zeros(1, 3, 640, 640, device="cuda", dtype=torch.float16)
                    )
                if isinstance(out, (tuple, list)):
                    out = out[0]
                if out.ndim != 3 or out.shape[-1] != 6:
                    raise RuntimeError(
                        f"{name}: expected end-to-end xyxy/score/class output, got {out.shape}; incompatible export must not run"
                    )
            # Record artifact hashes so deployed checkpoints can be traced to this export.
            records.append(
                dict(
                    application=app,
                    model=name,
                    path=str(destination),
                    sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                    source=str(source),
                    torch=torch.__version__,
                )
            )
    (a.output / "manifest.json").write_text(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
