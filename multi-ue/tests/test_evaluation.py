import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evaluate", ROOT / "tools/evaluate_ue.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_all_transmitted_frames_and_late_receipt(tmp_path):
    records = [
        dict(type="transmitted", run_id="r", video_id="v", frame_id=i, tx_mono_ns=0)
        for i in range(3)
    ]
    records.extend(
        [
            dict(
                type="result_receipt",
                run_id="r",
                video_id="v",
                frame_id=0,
                outcome="completed",
                receipt_mono_ns=100_000_000,
            ),
            dict(
                type="result_receipt",
                run_id="r",
                video_id="v",
                frame_id=1,
                outcome="completed",
                receipt_mono_ns=200_000_000,
            ),
        ]
    )
    p = tmp_path / "events"
    p.write_text("\n".join(json.dumps(x) for x in records))
    r = module.evaluate(p)
    assert (
        r["transmitted_frames"] == 3
        and r["missing_results"] == 1
        and r["slo_compliance"] == 1 / 3
    )
