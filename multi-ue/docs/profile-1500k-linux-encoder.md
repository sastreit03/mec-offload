# Profile 1.5 Mbit/s with the Linux UE encoder

This procedure measures encoded I-frame and P-frame sizes at a requested
bitrate of 1,500,000 bit/s. It invokes the same `Encoder` class used by the
Linux UE in `ue/linux/src/mec_ue/media.py`; it does not approximate the encoder
with FFmpeg or another implementation.

Run the profiling commands on the AGX Orin that runs the Linux UE. The gNB,
MEC, and softUE client are not required. Do not run a campaign concurrently,
because competing CPU and storage load would make the profiling environment
less reproducible.

## Measurement conditions

Keep these conditions equal to the existing encoding profile:

| Setting | Value |
|---|---:|
| Resolution | 1280x720 |
| Frame rate | 30 FPS |
| GOP length | 30 frames |
| B frames | disabled by the production encoder |
| Encoder | GStreamer `x264enc` |
| Encoder tuning | `zerolatency` |
| Speed preset | `veryfast` |
| H.264 profile | baseline |
| Requested bitrate | 1,500,000 bit/s |

Use the same eight videos used for the existing bitrate measurements. Changing
the videos changes the frame-size distribution and makes the new mean I/P
sizes less directly comparable with the values in `config/profiles.measured.yaml`.

The expected input filenames are:

```text
00cea101-06f21d5e.mov
01460ec4-ae03e419.mov
01c3e892-a4a8f9ec.mov
01f2af00-195831fa.mov
022752db-f5e0179c.mov
0245bba2-e3e6e1c0.mov
02927def-14c4c3cb.mov
029e2aad-f295cf71.mov
```

## 1. Prepare the Orin checkout

On the Orin:

```bash
cd ~/mec-offload-ad/multi-ue
git status --short

mkdir -p datasets/profile-videos
```

If the eight videos are still on the MEC Spark, copy them to the Orin. This is
the only step that connects to the MEC Spark:

```bash
rsync -av \
  sstreit@130.127.199.125:~/mec-offload-ad/multi-ue/datasets/profile-videos/ \
  datasets/profile-videos/
```

Verify that the input directory contains exactly the intended files:

```bash
cat > /tmp/aetheredge-expected-profile-videos.txt <<'EOF'
00cea101-06f21d5e.mov
01460ec4-ae03e419.mov
01c3e892-a4a8f9ec.mov
01f2af00-195831fa.mov
022752db-f5e0179c.mov
0245bba2-e3e6e1c0.mov
02927def-14c4c3cb.mov
029e2aad-f295cf71.mov
EOF

find datasets/profile-videos -maxdepth 1 -type f -printf '%f\n' |
  sort > /tmp/aetheredge-observed-profile-videos.txt

diff -u \
  /tmp/aetheredge-expected-profile-videos.txt \
  /tmp/aetheredge-observed-profile-videos.txt
```

`diff` must print nothing. If the original eight videos cannot be recovered,
stop and select a documented replacement set before profiling. Do not silently
mix the campaign-v2 videos with the original profiling set.

## 2. Select the production Linux UE image

The profiling container needs OpenCV, GStreamer, `x264enc`, and the Python GI
bindings. Use the same image used by the Linux UE:

```bash
UE_IMAGE="oai-nr-ue-cuda-mec:latest"

docker image inspect "$UE_IMAGE" >/dev/null
echo "Using Linux UE image: $UE_IMAGE"
```

If the deployed image has a different name, obtain it while the UE container
exists:

```bash
UE_IMAGE="$(docker inspect -f '{{.Config.Image}}' oai-nr-ue)"
echo "Using Linux UE image: $UE_IMAGE"
```

## 3. Run the 1.5 Mbit/s encoding profile

Keep `PROFILE_DIR` in the current shell for the remaining steps:

```bash
cd ~/mec-offload-ad/multi-ue

PROFILE_DIR="runs/profiling/encoding-1500k-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PROFILE_DIR"

docker run --rm \
  --entrypoint python3 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e PYTHONPATH=/multi-ue/contracts/python:/multi-ue/ue/linux/src \
  -v "$PWD:/multi-ue" \
  -w /multi-ue \
  "$UE_IMAGE" \
  tools/profile_encoding.py \
    --video-dir datasets/profile-videos \
    --bitrate 1500000 \
    --width 1280 \
    --height 720 \
    --fps 30 \
    --gop-frames 30 \
    --output "$PROFILE_DIR"
```

The command writes:

- `profile.json`: inputs and encoder conditions;
- `frame-sizes.csv`: one row per encoded access unit, including bitrate,
  frame type, GOP position, and encoded bytes.

The profiler forces an IDR at every 30-frame GOP boundary and fails if the
encoder does not produce the requested keyframe.

## 4. Validate every encoded frame and create the summary

```bash
python3 - "$PROFILE_DIR/frame-sizes.csv" "$PROFILE_DIR/summary.csv" <<'PY'
import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])
rows = list(csv.DictReader(source.open()))

if not rows:
    raise SystemExit("frame-sizes.csv is empty")

bitrates = {int(row["bitrate_bps"]) for row in rows}
if bitrates != {1_500_000}:
    raise SystemExit(f"unexpected bitrates: {sorted(bitrates)}")

invalid = []
by_video = defaultdict(list)
by_type = defaultdict(list)

for row in rows:
    frame_id = int(row["frame_id"])
    gop_frame = int(row["gop_frame"])
    frame_type = row["frame_type"]
    encoded_bytes = int(row["encoded_bytes"])

    if frame_type not in {"I", "P"} or encoded_bytes <= 0:
        invalid.append((row["video"], frame_id, "invalid type or size"))
    if gop_frame == 0 and frame_type != "I":
        invalid.append((row["video"], frame_id, "GOP boundary is not I"))

    by_video[row["video"]].append(frame_id)
    by_type[frame_type].append(encoded_bytes)

for video, frame_ids in by_video.items():
    if frame_ids != list(range(len(frame_ids))):
        invalid.append((video, None, "non-contiguous frame IDs"))

if len(by_video) != 8:
    invalid.append(("all", None, f"expected 8 videos, observed {len(by_video)}"))

if invalid:
    for problem in invalid[:20]:
        print("FAIL", *problem)
    raise SystemExit(f"encoding validation failed with {len(invalid)} errors")

def percentile(values, probability):
    values = sorted(values)
    position = (len(values) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)

fields = [
    "bitrate_bps",
    "frame_type",
    "samples",
    "mean_bytes",
    "p50_bytes",
    "p95_bytes",
    "p99_bytes",
]

summary = []
for frame_type in ("I", "P"):
    values = by_type[frame_type]
    if not values:
        raise SystemExit(f"no {frame_type}-frame samples")
    summary.append({
        "bitrate_bps": 1_500_000,
        "frame_type": frame_type,
        "samples": len(values),
        "mean_bytes": statistics.mean(values),
        "p50_bytes": percentile(values, 0.50),
        "p95_bytes": percentile(values, 0.95),
        "p99_bytes": percentile(values, 0.99),
    })

with output.open("w", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(summary)

total_bytes = sum(int(row["encoded_bytes"]) for row in rows)
duration_s = len(rows) / 30
produced_bps = total_bytes * 8 / duration_s

print("videos:", len(by_video))
print("encoded frames:", len(rows))
for item in summary:
    print(
        item["frame_type"],
        "samples=" + str(item["samples"]),
        f"mean_bytes={item['mean_bytes']:.3f}",
        f"p50_bytes={item['p50_bytes']:.3f}",
        f"p95_bytes={item['p95_bytes']:.3f}",
        f"p99_bytes={item['p99_bytes']:.3f}",
    )
print(f"requested_bps=1500000 produced_bps={produced_bps:.0f}")
print(f"produced/requested={produced_bps / 1_500_000:.4f}")
print("Encoding profile validation: PASS")
PY
```

The produced/requested ratio is a sanity check, not a requirement that every
short GOP or individual frame hit exactly 1.5 Mbit/s. `x264enc` rate control
operates over time, and video complexity changes individual frame sizes.

Review the generated aggregate values:

```bash
cat "$PROFILE_DIR/summary.csv"
cat "$PROFILE_DIR/profile.json"
```

The `mean_bytes` values are the values that correspond to
`frame_bytes_I` and `frame_bytes_P` in the measured runtime profile.

## 5. Preserve provenance

```bash
(
  cd "$PROFILE_DIR"
  sha256sum profile.json frame-sizes.csv summary.csv > SHA256SUMS
  sha256sum -c SHA256SUMS
)

echo "Profile directory: $PROFILE_DIR"
du -sh "$PROFILE_DIR"
```

Keep all four files together:

```text
profile.json
frame-sizes.csv
summary.csv
SHA256SUMS
```

## 6. Optionally combine the raw measurements

Do not overwrite the existing measured artifact. Create a new combined file:

```bash
BASE="runs/profiling/measured-20261028-120845/encoding/frame-sizes.csv"
COMBINED="$PROFILE_DIR/frame-sizes-with-existing-bitrates.csv"

python3 - "$BASE" "$PROFILE_DIR/frame-sizes.csv" "$COMBINED" <<'PY'
import csv
import sys
from pathlib import Path

base, added, output = map(Path, sys.argv[1:])
base_rows = list(csv.DictReader(base.open()))
added_rows = list(csv.DictReader(added.open()))

if any(int(row["bitrate_bps"]) == 1_500_000 for row in base_rows):
    raise SystemExit("base artifact already contains 1.5 Mbit/s rows")

fields = list(base_rows[0])
if list(added_rows[0]) != fields:
    raise SystemExit("CSV schemas differ")

with output.open("w", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(base_rows)
    writer.writerows(added_rows)

print(f"wrote {len(base_rows) + len(added_rows)} rows to {output}")
PY
```

## 7. Runtime integration is a separate step

This procedure only measures encoder frame sizes. It does not make 1.5 Mbit/s
selectable in a campaign.

Before regenerating `config/profiles.measured.yaml`, the catalog must contain
the intended 1.5 Mbit/s candidates. Detection and segmentation candidates also
need quality measurements at 1.5 Mbit/s because
`tools/build_collection_profiles.py` refuses candidates without corresponding
quality evidence. Tracking quality remains marked as unavailable because the
current project has no HOTA ground truth.

Do not manually edit the generated measured profile or substitute interpolated
I/P sizes. After the catalog and quality artifacts exist, regenerate it with
`tools/build_collection_profiles.py` using the combined frame-size CSV and
preserve the new source hashes in the generated YAML.

