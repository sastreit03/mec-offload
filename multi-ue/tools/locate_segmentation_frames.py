#!/usr/bin/env python3
"""Locate each BDD semantic annotation image in its corresponding source video."""

import argparse
import csv
import json
from pathlib import Path

def candidates(frame, target_shape):
    """Return plausible display rotations without distorting the aspect ratio."""
    th, tw = target_shape[:2]
    fh, fw = frame.shape[:2]
    if (fh, fw) == (th, tw):
        return [("none", frame)]
    if (fw, fh) == (th, tw):
        return [
            ("clockwise", cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)),
            ("counterclockwise", cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)),
        ]
    # Unusual metadata paths are still comparable, but the recorded resize is explicit.
    return [("resized", cv2.resize(frame, (tw, th)))]


def signature(image):
    # Downsampled luma is insensitive to small JPEG chroma differences.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--target-seconds", type=float, default=10.0)
    parser.add_argument("--window-seconds", type=float, default=3.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    global cv2, np
    import cv2
    import numpy as np

    source_manifest = json.loads(args.manifest.read_text())
    matched = []
    for example in source_manifest["examples"]:
        video = Path(example["video"])
        annotated = cv2.imread(str(args.dataset_root / example["image"]))
        if annotated is None:
            raise RuntimeError(f"unreadable annotation image: {example['image']}")
        target_signature = signature(annotated)

        capture = cv2.VideoCapture(str(video))
        if not capture.isOpened():
            raise RuntimeError(f"unreadable source video: {video}")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or frame_count <= 0:
            raise RuntimeError(f"invalid video metadata: {video}")
        center = round(args.target_seconds * fps)
        radius = max(1, round(args.window_seconds * fps))
        first = max(0, center - radius)
        last = min(frame_count - 1, center + radius)
        capture.set(cv2.CAP_PROP_POS_FRAMES, first)

        scores = []
        for frame_id in range(first, last + 1):
            ok, frame = capture.read()
            if not ok:
                break
            for rotation, displayed in candidates(frame, annotated.shape):
                score = float(
                    np.mean(
                        np.abs(
                            signature(displayed).astype(np.int16)
                            - target_signature.astype(np.int16)
                        )
                    )
                )
                scores.append((score, frame_id, rotation))
        capture.release()
        if not scores:
            raise RuntimeError(f"no searchable frames: {video}")

        scores.sort()
        best_score, best_frame, rotation = scores[0]
        # Ignore adjacent frames when judging whether the scene match is distinctive.
        alternatives = [item for item in scores if abs(item[1] - best_frame) > 2]
        second_score = alternatives[0][0] if alternatives else scores[-1][0]
        matched.append(
            {
                **example,
                "source_fps": fps,
                "source_frame_count": frame_count,
                "nominal_frame_id": center,
                "search_first_frame": first,
                "search_last_frame": last,
                "source_frame_id": best_frame,
                "display_rotation": rotation,
                "match_mae": best_score,
                "next_distinct_mae": second_score,
                "match_margin": second_score - best_score,
            }
        )

    args.output.mkdir(parents=True, exist_ok=True)
    enriched = dict(source_manifest, frame_location={
        "target_seconds": args.target_seconds,
        "window_seconds": args.window_seconds,
        "metric": "320x180 luma mean absolute error",
    }, examples=matched)
    (args.output / "manifest.json").write_text(json.dumps(enriched, indent=2))
    fields = [
        "id", "video", "source_fps", "source_frame_count", "nominal_frame_id",
        "source_frame_id", "display_rotation", "match_mae", "next_distinct_mae",
        "match_margin",
    ]
    with (args.output / "matches.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(matched)
    print(f"located annotated frames: {len(matched)}")


if __name__ == "__main__":
    main()
