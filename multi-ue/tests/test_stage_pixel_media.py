import json
import struct
import sys
from types import SimpleNamespace

import pytest
import yaml

import stage_pixel_media as stage


def assignment():
    return dict(ue_alias="pixel1", media_sha256="a" * 64, source_start_frame=3,
                frame_count=20, bitrate_bps=1000000)


def test_asset_identity_and_minimum_duration():
    ue = assignment()
    key, count = stage.asset_key(ue, 10, 10, 1280, 720)
    assert count == 80
    assert key == "px1-" + "a" * 64 + "-3-80-1280x720-10-10-1000000"
    assert stage.asset_key(dict(ue, frame_count=150), 10, 10, 1280, 720)[1] == 150
    with pytest.raises(ValueError, match="SHA-256"):
        stage.asset_key(dict(ue, media_sha256="bad"), 10, 10, 1280, 720)


@pytest.mark.parametrize("short_source", [False, True])
def test_asset_encoding_sequence_format_and_cleanup(tmp_path, monkeypatch, short_source):
    from mec_ue import media

    calls = []
    closed = []

    class Capture:
        position = 0
        def isOpened(self): return True
        def set(self, prop, value): self.position = value; return True
        def get(self, prop): return self.position
        def read(self):
            self.position += 1
            return (not short_source or self.position < 6), self.position - 1
        def release(self): closed.append("capture")

    class Encoder:
        def __init__(self, *args): pass
        def frame(self, image, i, fps, force, bitrate):
            calls.append((image, i, fps, force, bitrate))
            return [b"\x67a", b"\x68b", b"\x65c"] if force else [b"\x41d"]
        def close(self): closed.append("encoder")

    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        VideoCapture=lambda _: Capture(), CAP_PROP_POS_FRAMES=1,
        resize=lambda image, shape: image))
    monkeypatch.setattr(media, "Encoder", Encoder)
    path = tmp_path / "test.pav"
    if short_source:
        with pytest.raises(ValueError, match="ended before required frame"):
            stage.make_asset(path, "test-key", tmp_path / "source", 3, 12, 32, 24, 10, 10, 1000000)
    else:
        stage.make_asset(path, "test-key", tmp_path / "source", 3, 12, 32, 24, 10, 10, 1000000)
        assert [c[0] for c in calls] == list(range(3, 15))
        assert [c[1] for c in calls if c[3]] == [0, 10]
        with path.open("rb") as f:
            assert f.read(4) == b"PAV1"
            size = struct.unpack(">H", f.read(2))[0]
            assert f.read(size) == b"test-key"
            assert struct.unpack(">I", f.read(4))[0] == 12
            for i in range(12):
                size, flag = struct.unpack(">IB", f.read(5))
                assert flag == int(i % 10 == 0)
                assert f.read(size).startswith(b"\x00\x00\x00\x01")
            assert not f.read()
    assert closed == ["capture", "encoder"]


@pytest.mark.parametrize("stale", [False, True])
def test_offline_staging_receipt_check(tmp_path, monkeypatch, stale):
    catalog = tmp_path / "tasks.yaml"
    catalog.write_text(yaml.safe_dump(dict(source_hw=[720, 1280])))
    config = tmp_path / "campaign.yaml"
    config.write_text(yaml.safe_dump(dict(ues=dict(pixel1=dict(kind="android")))))
    manifest = tmp_path / "scenarios.jsonl"
    manifest.write_text(json.dumps(dict(fps=10, gop_frames=10, ues=[assignment()])) + "\n")
    lock = dict(scenarios_sha256=stage.digest(manifest), catalog_sha256=stage.digest(catalog),
                campaign_sha256=stage.digest(config))
    (tmp_path / "manifest.lock.json").write_text(json.dumps(lock))
    receipts = tmp_path / "pixel-staging"
    receipts.mkdir()
    key, _ = stage.asset_key(assignment(), 10, 10, 1280, 720)
    (receipts / "pixel1.json").write_text(json.dumps(dict(ue="pixel1", serial="device1",
        manifest_sha256="stale" if stale else lock["scenarios_sha256"],
        catalog_sha256=lock["catalog_sha256"], assets={key: key + "-" + "b" * 64 + ".pav"})))
    monkeypatch.setattr(sys, "argv", ["stage_pixel_media.py", "--campaign-dir", str(tmp_path),
        "--catalog", str(catalog), "--check-staged", str(config)])
    monkeypatch.setattr(stage.subprocess, "check_output", lambda *a, **kw: pytest.fail("Unexpected ADB call"))
    if stale:
        with pytest.raises(ValueError, match="stale"):
            stage.main()
    else:
        stage.main()
