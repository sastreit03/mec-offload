# Profile Linux UE encoding speed

This procedure measures whether the Linux UE's video-to-H.264 pipeline keeps up
with the requested FPS on the AGX Orin. It imports the production `Encoder` from
`ue/linux/src/mec_ue/media.py`, just like `profile_encoding.py`.

The script follows the sender's sequential processing and FPS-based release
schedule: read/decode, resize, encode, and account for output. It retains every
source frame and assigns media timestamps by frame index/FPS. When late, it
processes subsequent frames without waiting until it catches up. It does not
transmit packets, require the MEC/gNB/softUE, or test radio performance.

## Measurement conditions

| Setting | Value |
|---|---|
| Encoder | Production GStreamer `x264enc` (software encoder) |
| Tuning / speed preset | `zerolatency` / `veryfast` |
| H.264 profile / B frames | baseline / disabled |
| Frame retention | Every decoded frame, including at 10 FPS |
| Warmup | No frames excluded; first-frame costs remain visible |
| Deadline | Encoded output ready by the next scheduled frame release |
| Resolution | Width 1280, height 720 for the planned runs |

Use the same eight original videos as the bitrate profile. Do not run a campaign
concurrently. Keep the Orin power mode, cooling, and other workload consistent
between runs, and record those conditions with the results. Copy the new script
into the Orin checkout before running it; the checkout is mounted into the UE image.

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

## 3. Set the bitrates, GOP, FPS, and resolution

Run these commands in Bash. The first run uses 30 FPS and a 30-frame GOP:

```bash
GOP=30
FPS=30
WIDTH=1280
HEIGHT=720
BITRATES=(500000 1000000 1500000 2000000 4000000)
BITRATE_ARGS=()
for bitrate in "${BITRATES[@]}"; do
  BITRATE_ARGS+=(--bitrate "$bitrate")
done
```

For the second run, set `GOP=10` and `FPS=10`, then repeat steps 4–6 with a fresh
output directory. All settings are command-line arguments. The script defaults
to requiring eight videos; `--expected-videos` can explicitly change that count.

## 4. Run the speed profile

```bash
cd ~/mec-offload-ad/multi-ue
PROFILE_DIR="runs/profiling/encoding-speed-linux-${FPS}fps-${GOP}gop-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PROFILE_DIR"

docker run --rm \
  --entrypoint python3 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e PYTHONPATH=/multi-ue/contracts/python:/multi-ue/ue/linux/src \
  -v "$PWD:/multi-ue" \
  -w /multi-ue \
  "$UE_IMAGE" \
  tools/profile_encoding_speed.py \
    --video-dir datasets/profile-videos \
    "${BITRATE_ARGS[@]}" \
    --width "$WIDTH" \
    --height "$HEIGHT" \
    --fps "$FPS" \
    --gop-frames "$GOP" \
    --expected-videos 8 \
    --output "$PROFILE_DIR"
```

The output directory must be empty or new, preventing accidental overwrites.
Each video/bitrate pair starts a fresh encoder. IDRs are requested at frame zero
and each GOP boundary; missing requested IDRs or empty output fail the run.
Frame counts must agree across bitrates. A successful `capture.read()` is treated
as one source frame; a failed read ends that video, as in the existing profiler.
Compare counts with the bitrate profiles to help detect truncated input decoding.

Because this test is paced, it will take approximately the total assigned media
duration, or longer if processing cannot keep up. For eight approximately
40-second, 30 FPS videos and five bitrates, allow roughly 27 minutes at 30 FPS
and 80 minutes at 10 FPS. Lowering FPS retains frames and stretches media time.

## 5. Review timing and FPS requirements

The script writes its own summary; do not run `profile_encoding_validation.py`
on these timing artifacts.

- `profile.json`: settings, source/code hashes, environment, completion status,
  and `all_streams_meet_frame_deadlines`.
- `frame-timings.csv`: one row per frame, including I/P label, bytes, timestamps,
  stage durations, start lateness, and deadline lateness.
- `summary.csv`: one row per video/bitrate, with mean, p95, p99, and maximum
  timings, I/P encoder timings, throughput estimates, and deadline misses.

Print a compact report using only the host's Python standard library:

```bash
python3 - "$PROFILE_DIR" <<'PYREPORT'
import csv
import json
import sys
from pathlib import Path
p = Path(sys.argv[1])
meta = json.loads((p / "profile.json").read_text())
print("Run status:", meta["status"])
with (p / "summary.csv").open() as stream:
    for row in csv.DictReader(stream):
        print(Path(row["video"]).name,
              "bps=" + row["bitrate_bps"],
              "encoder_mean_ms=" + row["encoder_call_mean_ms"],
              "pipeline_p99_ms=" + row["processing_p99_ms"],
              "deadline_misses=" + row["deadline_misses"] + "/" + row["frames"],
              "final_lateness_ms=" + row["final_deadline_lateness_ms"])
PYREPORT
```

Interpret the measurements as follows:

| Field | Meaning |
|---|---|
| `decode_ns` | `capture.read()`: source reading and decoding |
| `resize_ns` | OpenCV resize |
| `encoder_call_ns` | Production `Encoder.frame()`: raw buffer copying, color conversion, encoding, and output extraction/parsing |
| `output_accounting_ns` | IDR check and canonical encoded-byte calculation |
| `processing_ns` | All four stages above; excludes pacing sleep |
| `start_lateness_ns` | How late reading started relative to the planned release |
| `release_to_ready_ns` | Time from planned release until output was ready |
| `deadline_lateness_ns` | Time beyond the next scheduled release; zero if on time |

CSV timestamps are monotonic nanoseconds relative to the beginning of that
video/bitrate stream. Summary timing values are milliseconds. Encoder setup is
reported separately as `setup_ms`. The first encoder call is included in timing
statistics. Timings are buffered in memory and written after each video so CSV
writes do not stall the frame loop. Python measurement bookkeeping and scheduler
delays can still contribute to lateness.

At **30 FPS**, each frame has **33.33 ms**; at **10 FPS**, it has **100 ms**.
`encoder_mean_within_budget` checks the encoder-call average only.
`processing_mean_within_budget` checks the measured pipeline average.
Neither proves that every frame meets its deadline.

`deadline_misses` counts frames whose output was not ready by the next scheduled
release, including startup and scheduler effects. `all_frames_ready_by_deadline`
is a strict zero-miss result. `final_deadline_lateness_ms` shows how far the last
output lagged the schedule; inspect per-frame lateness to distinguish occasional
spikes from growing backlog. There is no hidden jitter tolerance.

`processing_capacity_fps` and `encoder_call_capacity_fps` are the reciprocals of
mean measured service times, not measured unpaced maximum throughput.
`paced_output_fps` is frames divided by elapsed time to the final output; it can
slightly exceed the target because frame zero is released immediately. Use the
deadline fields to assess schedule compliance instead of this average alone.

A run with `status=complete` collected all measurements; it is **not** a speed
PASS. Even zero deadline misses establish only that the offline pipeline met this
test's deadlines. Production adds RTP packetization, socket submission, other
application work, and radio contention. Require headroom and verify actual
production transmission cadence before concluding the complete UE meets FPS.

## 6. Preserve provenance

```bash
docker image inspect "$UE_IMAGE" --format '{{.Id}}' > "$PROFILE_DIR/ue-image-id.txt"
(
  cd "$PROFILE_DIR"
  sha256sum profile.json frame-timings.csv summary.csv ue-image-id.txt > SHA256SUMS
  sha256sum -c SHA256SUMS
)
echo "Profile directory: $PROFILE_DIR"
```

Keep those five files together. Source hashes in `profile.json` identify the
videos; code hashes identify the profiler and production encoder implementation.
This procedure does not change campaign configurations or measured model profiles.
