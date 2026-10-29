from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for p in [
    ROOT / "contracts/python",
    ROOT / "mec/src",
    ROOT / "ue/linux/src",
    ROOT / "tools",
    ROOT.parent / "EdgeRIC-v2",
]:
    sys.path.insert(0, str(p))
