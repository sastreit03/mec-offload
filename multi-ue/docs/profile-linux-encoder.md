# Profile 1.5 Mbit/s with the Linux UE encoder

This procedure measures encoded I-frame and P-frame sizes at a requested
bitrate. It invokes the same `Encoder` class used by the
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
| B frames | disabled by the production encoder |
| Encoder | GStreamer `x264enc` |
| Encoder tuning | `zerolatency` |
| Speed preset | `veryfast` |
| H.264 profile | baseline |

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

## 3. Set the bitrate, GOP, frame rate, and resolution

Set the desired values below. Bitrate is in bits per second, GOP is in frames,
FPS is in frames per second, and width and height are in pixels. Keep these
variables in the same shell for the remaining steps:

```bash
GOP=<GOP_IN_FRAMES, e.g., 30>
FPS=<FRAMES_PER_SECOND, e.g., 30>
WIDTH=<WIDTH_IN_PIXELS, e.g., 1280>
HEIGHT=<HEIGHT_IN_PIXELS, e.g., 720>

# Replace with with desired bitrates
BITRATES=(500000 1000000 1500000 2000000 4000000)

BITRATE_ARGS=()
for bitrate in "${BITRATES[@]}"; do
  BITRATE_ARGS+=(--bitrate "$bitrate")
done
```

## 4. Run the encoding profile at the desired settings for bitrate, GOP, FPS, and resolution

Keep `PROFILE_DIR` in the current shell for the remaining steps:

```bash
cd ~/mec-offload-ad/multi-ue
PROFILE_DIR="runs/profiling/encoding-linux-${FPS}fps-${GOP}gop-$(date +%Y%m%d-%H%M%S)"
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
    "${BITRATE_ARGS[@]}" \
    --width "$WIDTH" \
    --height "$HEIGHT" \
    --fps "$FPS" \
    --gop-frames "$GOP" \
    --output "$PROFILE_DIR"
```

The command writes:

- `profile.json`: inputs and encoder conditions;
- `frame-sizes.csv`: one row per encoded access unit, including bitrate,
  frame type, GOP position, and encoded bytes.

The profiler forces an IDR at every \<GOP\>-frame GOP boundary and fails if the
encoder does not produce the requested keyframe.

## 5. Validate every encoded frame and create the summary

Run the validator on the Orin host; it uses only the Python standard library.
Pass the same settings used for encoding:

```bash
python3 tools/profile_encoding_validation.py \
  --profile-dir "$PROFILE_DIR" \
  "${BITRATE_ARGS[@]}" \
  --width "$WIDTH" \
  --height "$HEIGHT" \
  --fps "$FPS" \
  --gop-frames "$GOP" \
  --expected-videos 8
```

The validator checks the requested settings against `profile.json`, including
resolution, which is not stored in the per-frame CSV. It checks every expected
video at every bitrate, contiguous frame IDs, consistent frame counts across
bitrates, GOP positions, positive sizes, and I frames at GOP boundaries.

It writes `summary.csv` with I/P size statistics separately for each bitrate
and prints per-video and aggregate produced/requested bitrate ratios. For
`GOP=1`, only I-frame statistics are written because every frame is intra-coded.

Produced bitrate uses encoded media duration (`frame_count / FPS`), not the
wall-clock runtime of the profiling command. The produced/requested ratio is a
sanity check, not a requirement that every short GOP or individual frame hit
the target bitrate. A validation PASS checks the artifact structure and frame
rules; it does not enforce a bitrate-error tolerance. `x264enc` rate control
operates over time, and video complexity changes individual frame sizes.

Review the generated aggregate values:

```bash
cat "$PROFILE_DIR/summary.csv"
cat "$PROFILE_DIR/profile.json"
```

The `mean_bytes` values are the values that correspond to
`frame_bytes_I` and `frame_bytes_P` in the measured runtime profile.

## 6. Preserve provenance

```bash
(
  cd "$PROFILE_DIR"
  sha256sum profile.json frame-sizes.csv summary.csv bitrate_comparison.csv \
    > SHA256SUMS
  sha256sum -c SHA256SUMS
)

echo "Profile directory: $PROFILE_DIR"
du -sh "$PROFILE_DIR"
```

Keep all five files together:

```text
bitrate_comparison.csv
profile.json
frame-sizes.csv
summary.csv
SHA256SUMS
```

## 7. Optionally combine the raw measurements

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

## 8. Runtime integration is a separate step

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

