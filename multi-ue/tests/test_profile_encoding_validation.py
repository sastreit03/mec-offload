import csv
import json

import pytest

from profile_encoding_validation import main


def artifacts(tmp_path, gop=2):
    (tmp_path / "profile.json").write_text(json.dumps({
        "videos": ["a.mov", "b.mov"], "bitrates_bps": [500000, 1500000],
        "fps": 10, "gop_frames": gop, "width": 1280, "height": 720,
    }))
    rows = [
        dict(video=video, bitrate_bps=rate, frame_id=fid,
             gop_id=fid // gop, gop_frame=fid % gop,
             frame_type="I" if fid % gop == 0 else "P",
             encoded_bytes=100 if rate == 500000 else 300)
        for rate in (500000, 1500000)
        for video in ("a.mov", "b.mov")
        for fid in range(4)
    ]
    write_rows(tmp_path, rows)
    return rows, [
        "--profile-dir", str(tmp_path), "--bitrate", "500000",
        "--bitrate", "1500000", "--fps", "10", "--gop-frames", str(gop),
        "--width", "1280", "--height", "720", "--expected-videos", "2",
    ]


def write_rows(tmp_path, rows):
    with (tmp_path / "frame-sizes.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_multiple_bitrates_and_ten_fps(tmp_path, capsys):
    _, args = artifacts(tmp_path)
    main(args)
    with (tmp_path / "summary.csv").open() as stream:
        summary = list(csv.DictReader(stream))
    assert [(r["bitrate_bps"], r["frame_type"], r["samples"], r["mean_bytes"])
            for r in summary] == [
        ("500000", "I", "4", "100"), ("500000", "P", "4", "100"),
        ("1500000", "I", "4", "300"), ("1500000", "P", "4", "300"),
    ]
    output = capsys.readouterr().out
    for video in ("a.mov", "b.mov"):
        assert (f"video={video} encoded_bytes=400 frames=4 produced_bps=8000 "
                "produced/requested=0.0160") in output
        assert (f"video={video} encoded_bytes=1200 frames=4 produced_bps=24000 "
                "produced/requested=0.0160") in output
    assert "aggregate encoded_bytes=800 frames=8" in output
    assert "aggregate encoded_bytes=2400 frames=8" in output
    assert "media_duration_s=0.800 produced_bps=8000" in output
    assert "media_duration_s=0.800 produced_bps=24000" in output
    with (tmp_path / "bitrate_comparison.csv").open() as stream:
        comparisons = list(csv.DictReader(stream))
    assert len(comparisons) == 6
    for rate, size, produced in ((500000, 400, 8000), (1500000, 1200, 24000)):
        rows = [r for r in comparisons if int(r["requested_bps"]) == rate]
        assert [(r["scope"], r["video"]) for r in rows] == [
            ("video", "a.mov"), ("video", "b.mov"), ("aggregate", ""),
        ]
        for row in rows:
            factor = 2 if row["scope"] == "aggregate" else 1
            assert int(row["encoded_bytes"]) == size * factor
            assert int(row["frames"]) == 4 * factor
            assert float(row["media_duration_s"]) == 0.4 * factor
            assert float(row["produced_bps"]) == produced
            assert float(row["produced_requested_ratio"]) == produced / rate


@pytest.mark.parametrize("problem", ["missing_video", "duplicate", "gop", "idr", "resolution", "truncated"])
def test_rejects_invalid_artifacts(tmp_path, problem):
    rows, args = artifacts(tmp_path)
    if problem == "missing_video":
        rows = rows[:-4]
    elif problem == "duplicate":
        rows[1]["frame_id"] = 0
    elif problem == "gop":
        rows[1]["gop_frame"] = 0
    elif problem == "idr":
        rows[0]["frame_type"] = "P"
    elif problem == "resolution":
        args[args.index("--width") + 1] = "640"
    elif problem == "truncated":
        rows = rows[:-1]
    write_rows(tmp_path, rows)
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 1
    assert not (tmp_path / "summary.csv").exists()


def test_all_intra_gop(tmp_path):
    _, args = artifacts(tmp_path, gop=1)
    main(args)
    with (tmp_path / "summary.csv").open() as stream:
        summary = list(csv.DictReader(stream))
    assert len(summary) == 2
    assert all(row["frame_type"] == "I" for row in summary)


def pixel_artifacts(tmp_path):
    rows, args = artifacts(tmp_path)
    meta_path = tmp_path / "profile.json"
    meta = json.loads(meta_path.read_text())
    meta.update(encoder="android_mediacodec_offline_v1", status="complete")
    meta_path.write_text(json.dumps(meta))
    for row in rows:
        row["pts_us"] = row["frame_id"] * 100000
    write_rows(tmp_path, rows)
    streams = [dict(video=v, bitrate_bps=r, submitted_frames=4, encoded_frames=4, encoder_eos=True)
               for r in (500000, 1500000) for v in ("a.mov", "b.mov")]
    (tmp_path / "streams.jsonl").write_text("".join(json.dumps(s) + "\n" for s in streams))
    return rows, args


def test_pixel_profile_and_extra_idrs(tmp_path):
    rows, args = pixel_artifacts(tmp_path)
    rows[1]["frame_type"] = "I"
    write_rows(tmp_path, rows)
    main(args)
    with (tmp_path / "keyframe_summary.csv").open() as stream:
        keyframes = list(csv.DictReader(stream))
    assert keyframes[0]["extra_idr_frames"] == "1"
    assert keyframes[0]["idr_positions"] == "[0, 1, 2]"
    with (tmp_path / "bitrate_windows.csv").open() as stream:
        windows = list(csv.DictReader(stream))
    assert len(windows) == 4
    assert windows[0]["partial_window"] == "True"
    assert float(windows[0]["produced_bps"]) == 8000


def test_full_and_partial_bitrate_windows(tmp_path):
    _, args = artifacts(tmp_path, gop=10)
    rows = [dict(video=v, bitrate_bps=r, frame_id=f, gop_id=f // 10,
                 gop_frame=f % 10, frame_type="I" if f % 10 == 0 else "P",
                 encoded_bytes=100 if f < 50 else 200)
            for r in (500000, 1500000) for v in ("a.mov", "b.mov")
            for f in range(60)]
    write_rows(tmp_path, rows)
    main(args)
    with (tmp_path / "bitrate_windows.csv").open() as stream:
        windows = list(csv.DictReader(stream))
    assert len(windows) == 8
    assert [(r["frames"], r["partial_window"], float(r["produced_bps"]))
            for r in windows[:2]] == [("50", "False", 8000), ("10", "True", 16000)]


@pytest.mark.parametrize("problem", ["timestamp", "running", "missing_drain", "drain_count", "eos"])
def test_pixel_rejects_incomplete_evidence(tmp_path, problem):
    rows, args = pixel_artifacts(tmp_path)
    if problem == "timestamp":
        rows[1]["pts_us"] = 33333
        write_rows(tmp_path, rows)
    elif problem == "running":
        p = tmp_path / "profile.json"
        m = json.loads(p.read_text()); m["status"] = "running"
        p.write_text(json.dumps(m))
    elif problem == "missing_drain":
        (tmp_path / "streams.jsonl").write_text("")
    else:
        p = tmp_path / "streams.jsonl"
        streams = [json.loads(line) for line in p.read_text().splitlines()]
        streams[0]["encoded_frames" if problem == "drain_count" else "encoder_eos"] = (
            3 if problem == "drain_count" else False)
        p.write_text("".join(json.dumps(s) + "\n" for s in streams))
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 1
