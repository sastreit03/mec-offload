from pathlib import Path
from types import SimpleNamespace

import pytest

from profile_encoding_speed import main, measure_stream, summarize


def pipeline(encode_ms, frames=4, bad_idr=False):
    class Clock:
        now = 0

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.now += round(seconds * 1e9)

    clock = Clock()
    state = SimpleNamespace(read=0, closed=False, released=False, requests=[])

    class Capture:
        def isOpened(self):
            return True

        def read(self):
            if state.read == frames:
                return False, None
            state.read += 1
            clock.now += 5_000_000
            return True, object()

        def release(self):
            state.released = True

    def resize(bgr, size):
        clock.now += 5_000_000
        return bgr

    class Encoder:
        def __init__(self, *args):
            clock.now += 20_000_000

        def frame(self, bgr, index, fps, keyframe, bitrate):
            state.requests.append((index, fps, keyframe, bitrate))
            clock.now += round(encode_ms * 1e6)
            return [b"\x65" if keyframe and not bad_idr else b"\x41"]

        def close(self):
            state.closed = True

    cv2 = SimpleNamespace(VideoCapture=lambda _: Capture(), resize=resize)
    args = SimpleNamespace(width=1280, height=720, fps=10, gop_frames=2)
    return clock, state, cv2, Encoder, args


@pytest.mark.parametrize("encode_ms, misses, last_late_ms", [(20, 0, 0), (120, 4, 120)])
def test_paced_pipeline_and_backlog(encode_ms, misses, last_late_ms):
    clock, state, cv2, encoder, args = pipeline(encode_ms)
    rows, setup = measure_stream(Path("a.mov"), 1500000, args, cv2, encoder,
                                 lambda nals: b"".join(nals), clock, clock.sleep)
    summary = summarize(rows, 10, setup)
    assert summary["setup_ms"] == 20
    assert summary["encoder_call_mean_ms"] == encode_ms
    assert summary["processing_mean_ms"] == encode_ms + 10
    assert summary["deadline_misses"] == misses
    assert summary["final_deadline_lateness_ms"] == last_late_ms
    assert [r["scheduled_release_ns"] for r in rows] == [0, 100000000, 200000000, 300000000]
    assert [r["read_start_ns"] for r in rows] == [
        i * max(100, encode_ms + 10) * 1000000 for i in range(4)]
    assert state.requests == [(i, 10, i % 2 == 0, 1500000) for i in range(4)]
    assert state.closed and state.released


def test_missing_keyframe_releases_resources():
    clock, state, cv2, encoder, args = pipeline(20, bad_idr=True)
    with pytest.raises(RuntimeError, match="missing requested IDR"):
        measure_stream(Path("a.mov"), 500000, args, cv2, encoder,
                       lambda nals: b"".join(nals), clock, clock.sleep)
    assert state.closed and state.released


def test_refuses_overwrite_before_loading_codec(tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "a.mov").touch()
    output = tmp_path / "results"
    output.mkdir()
    (output / "profile.json").write_text("existing results")
    with pytest.raises(SystemExit) as error:
        main(["--video-dir", str(videos), "--expected-videos", "1",
              "--bitrate", "500000", "--bitrate", "1500000", "--output", str(output)])
    assert error.value.code == 2
    assert (output / "profile.json").read_text() == "existing results"
