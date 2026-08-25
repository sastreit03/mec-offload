from __future__ import annotations

import importlib
import sys


def check(label: str, module: str) -> bool:
    try:
        m = importlib.import_module(module)
        version = getattr(m, "__version__", "available")
        print(f"[OK] {label}: {version}")
        return True
    except Exception as exc:
        print(f"[FAIL] {label}: {exc}")
        return False


def main() -> int:
    ok = True
    ok &= check("PyGObject/GI", "gi")
    ok &= check("NumPy", "numpy")
    ok &= check("Pillow", "PIL")
    ok &= check("FastAPI", "fastapi")
    ok &= check("PyTorch", "torch")
    ok &= check("Transformers", "transformers")

    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        required = [
            "udpsrc", "rtpjitterbuffer", "rtph264depay", "h264parse",
            "avdec_h264", "videoconvert", "jpegenc", "appsink",
        ]
        missing = [name for name in required if Gst.ElementFactory.find(name) is None]
        if missing:
            print("[FAIL] Missing GStreamer elements:", ", ".join(missing))
            ok = False
        else:
            print("[OK] Required GStreamer elements are available")
    except Exception as exc:
        print(f"[FAIL] GStreamer initialization: {exc}")
        ok = False

    try:
        import torch
        print(f"CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"CUDA device: {torch.cuda.get_device_name(0)}")
        else:
            print("[FAIL] CUDA is not available to PyTorch")
            ok = False
    except Exception as exc:
        print(f"[FAIL] CUDA availability: {exc}")
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
