"""Collection permits absent quality, but never absent execution evidence."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


TOOL = Path(__file__).resolve().parents[1] / "tools/build_collection_profiles.py"


def build(tmp_path, missing=None):
    candidates = [
        dict(id=f"{app}-{rate}", application=app, model=model, bitrate_bps=rate)
        for app, model in (("detection", "det"), ("segmentation", "seg"), ("tracking", "det"))
        for rate in (500000, 1500000)
    ]
    (tmp_path / "catalog.yaml").write_text(yaml.safe_dump(dict(configurations=candidates)))
    timing = "application,model,priority,processing_s,tracker_s\n"
    if missing != "processing":
        timing += "detection,det,-3,0.01,\n"
    timing += "segmentation,seg,-3,0.02,\n"
    timing += "tracking,det,-3,0.01," + ("" if missing == "tracker" else "0.003") + "\n"
    (tmp_path / "processing.csv").write_text(timing)
    frames = "bitrate_bps,frame_type,encoded_bytes\n"
    for rate in (500000, 1500000):
        for kind, size in (("I", 10000), ("P", 1000)):
            if rate == 1500000 and missing == kind:
                continue
            frames += f"{rate},{kind},{size}\n"
    (tmp_path / "frames.csv").write_text(frames)
    for app, model, metric in (("detection", "det", "map50_95"), ("segmentation", "seg", "miou")):
        score = "nan" if missing == "invalid_quality" else "0.4"
        (tmp_path / f"{app}.csv").write_text(
            f"model,bitrate_bps,{metric},examples\n"
            f"{model},0,0.8,10\n{model},500000,{score},10\n"
        )
    output = tmp_path / "profiles.yaml"
    command = [sys.executable, str(TOOL)]
    for flag, name in (
        ("catalog", "catalog.yaml"), ("processing", "processing.csv"),
        ("frame-sizes", "frames.csv"), ("detection-quality", "detection.csv"),
        ("segmentation-quality", "segmentation.csv"), ("output", "profiles.yaml"),
    ):
        command.extend([f"--{flag}", str(tmp_path / name)])
    return subprocess.run(command, capture_output=True, text=True), output


def test_missing_quality_is_explicit_and_measured_scores_are_preserved(tmp_path):
    result, output = build(tmp_path)
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(output.read_text())
    for app in ("detection", "segmentation"):
        missing = data["candidates"][f"{app}-1500000"]
        assert missing["quality_status"] == "not_measured"
        assert not {"score", "reference_score", "quality_examples", "metric", "dataset"} & missing.keys()
        assert missing["processing_s"] > 0
        assert missing["frame_bytes_I"] == 10000
        measured = data["candidates"][f"{app}-500000"]
        assert measured["quality_status"] == "measured"
        assert measured["score"] == 0.4
        assert measured["reference_score"] == 0.8
    assert all(p["qualified"] is False for p in data["candidates"].values())
    tracking = data["candidates"]["tracking-1500000"]
    assert tracking["tracker_s"] == 0.003
    assert tracking["quality_status"] == "not_measured_no_HOTA_ground_truth"
    assert data["provenance"]["missing_quality_candidates"] == [
        "detection-1500000", "segmentation-1500000"]
    for entry in data["provenance"]["sources"].values():
        assert entry["sha256"] == hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
    assert "Quality unmeasured" in result.stdout


@pytest.mark.parametrize("missing, message", [
    ("processing", "missing processing measurements"),
    ("tracker", "missing tracker measurements"),
    ("I", "missing I-frame measurements"),
    ("P", "missing P-frame measurements"),
    ("invalid_quality", "must be finite and positive"),
])
def test_missing_execution_or_invalid_supplied_quality_still_fails(tmp_path, missing, message):
    result, output = build(tmp_path, missing)
    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()
