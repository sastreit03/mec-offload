#!/usr/bin/env python3
"""Measure SegFormer mIoU after production UE H.264 encoding."""

import argparse
import csv
import json
from pathlib import Path


def decode_rle(runs, height, width, np):
    flat = np.empty(height * width, dtype=np.int32)
    offset = 0
    for value, count in runs:
        flat[offset : offset + count] = value
        offset += count
    if offset != flat.size:
        raise RuntimeError("segmentation RLE length mismatch")
    return flat.reshape(height, width)


def add_confusion(confusion, truth, prediction, np):
    valid = truth != 255
    if not np.any(valid):
        return
    gt = truth[valid].astype(np.int64)
    pred = prediction[valid].astype(np.int64)
    if np.any((gt < 0) | (gt >= 19)) or np.any((pred < 0) | (pred >= 19)):
        raise RuntimeError("class outside Cityscapes-19 range")
    confusion += np.bincount(gt * 19 + pred, minlength=19 * 19).reshape(19, 19)


def miou(confusion, np):
    intersection = np.diag(confusion).astype(np.float64)
    union = confusion.sum(0) + confusion.sum(1) - intersection
    present = union > 0
    return float(np.mean(intersection[present] / union[present])), int(present.sum())


class Decoder:
    """Decode complete Annex-B access units with the MEC's H.264 software path."""

    def __init__(self, fps):
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo

        Gst.init(None)
        self.Gst = Gst
        self.GstVideo = GstVideo
        self.fps = fps
        self.pipeline = Gst.parse_launch(
            'appsrc name=encoded is-live=false block=true format=time '
            'caps="video/x-h264,stream-format=byte-stream,alignment=au" ! '
            'h264parse ! avdec_h264 ! videoconvert ! '
            'video/x-raw,format=RGB ! '
            'appsink name=decoded max-buffers=1 drop=false sync=false '
            'wait-on-eos=false enable-last-sample=false'
        )
        self.source = self.pipeline.get_by_name("encoded")
        self.sink = self.pipeline.get_by_name("decoded")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("decoder pipeline setup failed")

    def _sample(self, sample, np):
        """Return the source frame ID and RGB pixels carried by one sample."""
        output = sample.get_buffer()
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        raw = output.extract_dup(0, output.get_size())
        source_frame_id = round(output.pts * self.fps / 1e9)
        rgb = (
            np.frombuffer(raw, dtype=np.uint8)
            .reshape(info.height, info.stride[0])[:, : info.width * 3]
            .reshape(info.height, info.width, 3)
            .copy()
        )
        return source_frame_id, rgb

    def frame(self, access_unit, frame_id, np):
        """Push one access unit and return a decoded frame when one is ready."""
        buffer = self.Gst.Buffer.new_allocate(None, len(access_unit), None)
        buffer.fill(0, access_unit)
        buffer.pts = round(frame_id * 1e9 / self.fps)
        buffer.duration = round(1e9 / self.fps)
        if self.source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
            raise RuntimeError("decoder input flow error")
        # Frame zero is expected to be retained; avoid a two-second wait there.
        timeout = 100 * self.Gst.MSECOND if frame_id == 0 else 2 * self.Gst.SECOND
        sample = self.sink.emit("try-pull-sample", timeout)
        if sample is None:
            # The parser normally holds the first frame until the next AU arrives.
            if frame_id == 0:
                return None
            raise RuntimeError(f"decoder timed out after access unit {frame_id}")
        return self._sample(sample, np)

    def finish(self, np):
        """Flush the final frame retained by the H.264 parser/decoder."""
        self.source.emit("end-of-stream")
        decoded = []
        while True:
            sample = self.sink.emit("try-pull-sample", 2 * self.Gst.SECOND)
            if sample is None:
                break
            decoded.append(self._sample(sample, np))
        return decoded

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("config/tasks.yaml"))
    parser.add_argument("--weights", type=Path, default=Path("weights"))
    parser.add_argument("--bitrate", type=int, action="append", required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--gop-frames", type=int, default=30)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import cv2
    import numpy as np
    from PIL import Image
    import yaml

    from mec_contracts.rtp import canonical_au
    from mec_server.workers import TorchAdapter

    import sys
    ue_source = Path(__file__).resolve().parents[1] / "ue" / "linux" / "src"
    sys.path.insert(0, str(ue_source))
    from mec_ue.media import Encoder

    manifest = json.loads(args.manifest.read_text())
    examples = manifest["examples"]
    if args.limit is not None:
        if args.limit <= 0:
            parser.error("limit must be positive")
        examples = examples[: args.limit]
    catalog = yaml.safe_load(args.catalog.read_text())
    specs = {}
    for configuration in catalog["configurations"]:
        if configuration["application"] == "segmentation":
            specs.setdefault(configuration["model"], configuration)
    if len(specs) != 4:
        raise RuntimeError("expected four segmentation models")
    bitrates = sorted(set(args.bitrate))
    if any(rate <= 0 for rate in bitrates):
        parser.error("bitrates must be positive")

    adapter = TorchAdapter(specs, args.weights, [0])
    conditions = [0] + bitrates
    counts = {
        (model, bitrate): np.zeros((19, 19), dtype=np.int64)
        for model in specs
        for bitrate in conditions
    }
    diagnostic_rows = []
    args.output.mkdir(parents=True, exist_ok=True)

    for number, example in enumerate(examples, 1):
        if example.get("display_rotation") != "none":
            raise RuntimeError("quality profiling requires the production display orientation")
        truth = np.asarray(Image.open(args.dataset_root / example["mask"]), dtype=np.uint8)
        annotated_bgr = cv2.imread(str(args.dataset_root / example["image"]))
        if annotated_bgr is None:
            raise RuntimeError(f"unreadable annotated image: {example['image']}")
        annotated_rgb = cv2.cvtColor(annotated_bgr, cv2.COLOR_BGR2RGB)

        # The annotation JPEG is the uncompressed reference condition.
        for model, configuration in specs.items():
            payload, _ = adapter.infer(
                {"priority": 0, "configuration": configuration, "rgb": annotated_rgb}
            )
            prediction = decode_rle(payload["runs"], payload["height"], payload["width"], np)
            add_confusion(counts[(model, 0)], truth, prediction, np)
            image_counts = np.zeros((19, 19), dtype=np.int64)
            add_confusion(image_counts, truth, prediction, np)
            score, classes = miou(image_counts, np)
            diagnostic_rows.append(
                {"id": example["id"], "model": model, "bitrate_bps": 0,
                 "miou": score, "classes": classes}
            )

        target = int(example["source_frame_id"])
        for bitrate in bitrates:
            capture = cv2.VideoCapture(str(example["video"]))
            if not capture.isOpened():
                raise RuntimeError(f"unreadable source video: {example['video']}")
            encoder = Encoder(1280, 720, args.fps, bitrate)
            decoder = Decoder(args.fps)
            decoded_rgb = None
            try:
                for frame_id in range(target + 1):
                    ok, bgr = capture.read()
                    if not ok:
                        raise RuntimeError(
                            f"video ended before matched frame: {example['id']}"
                        )
                    bgr = cv2.resize(bgr, (1280, 720))
                    nals = encoder.frame(
                        bgr,
                        frame_id,
                        args.fps,
                        keyframe=frame_id % args.gop_frames == 0,
                        bitrate=bitrate,
                    )
                    decoded = decoder.frame(canonical_au(nals), frame_id, np)
                    if decoded is not None and decoded[0] == target:
                        decoded_rgb = decoded[1]
                # End-of-stream releases a target frame retained by the decoder.
                if decoded_rgb is None:
                    for decoded_frame_id, rgb in decoder.finish(np):
                        if decoded_frame_id == target:
                            decoded_rgb = rgb
            finally:
                decoder.close()
                encoder.close()
                capture.release()
            if decoded_rgb is None:
                raise RuntimeError(f"no decoded target frame: {example['id']}")

            for model, configuration in specs.items():
                payload, _ = adapter.infer(
                    {"priority": 0, "configuration": configuration, "rgb": decoded_rgb}
                )
                prediction = decode_rle(
                    payload["runs"], payload["height"], payload["width"], np
                )
                add_confusion(counts[(model, bitrate)], truth, prediction, np)
                image_counts = np.zeros((19, 19), dtype=np.int64)
                add_confusion(image_counts, truth, prediction, np)
                score, classes = miou(image_counts, np)
                diagnostic_rows.append(
                    {"id": example["id"], "model": model,
                     "bitrate_bps": bitrate, "miou": score, "classes": classes}
                )
        print(f"completed {number}/{len(examples)}: {example['id']}", flush=True)

    summary_rows = []
    for model in specs:
        for bitrate in conditions:
            score, classes = miou(counts[(model, bitrate)], np)
            summary_rows.append(
                {"model": model, "bitrate_bps": bitrate, "miou": score,
                 "classes": classes, "examples": len(examples)}
            )
            np.save(args.output / f"confusion-{model}-{bitrate}.npy", counts[(model, bitrate)])

    with (args.output / "summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=summary_rows[0].keys())
        writer.writeheader()
        writer.writerows(summary_rows)
    with (args.output / "per-example.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=diagnostic_rows[0].keys())
        writer.writeheader()
        writer.writerows(diagnostic_rows)
    (args.output / "profile.json").write_text(
        json.dumps(
            {"manifest": str(args.manifest), "dataset_root": str(args.dataset_root),
             "catalog": str(args.catalog), "weights": str(args.weights),
             "bitrates_bps": bitrates, "fps": args.fps,
             "gop_frames": args.gop_frames, "metric": "mIoU",
             "ignore_class": 255},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
