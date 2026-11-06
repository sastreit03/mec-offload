# Profile Linux video submission over local UDP

Run this test on the AGX Orin with the production Linux UE image. It measures
reading/decoding, resizing, the production encoder, production RTP packetization,
and submission of every datagram to a local UDP socket. It uses the same eight
original videos as the encoding profiles.

The destination is fixed to `127.0.0.1`, with an automatically allocated port.
A separate receiver process continuously drains the socket and verifies the RTP
frames. No MEC, gNB, radio, external receiver, or clock synchronization is needed.
The Docker command below uses `--network none`; loopback remains available.

## Measurement conditions

| Setting | Value |
|---|---|
| Encoder | Linux production `Encoder`, GStreamer x264, zerolatency/veryfast |
| RTP | Production `Descriptor`, `packetize`, and 96-byte frame descriptor |
| Datagram limit | 1200 bytes, including RTP and extension, excluding UDP/IP headers |
| Schedule | FPS-paced, all source frames retained, catch up when late |
| Deadline | Last socket send returns before the next scheduled frame release |
| Warmup | No frames excluded |
| Receiver | Separate local Python process using production `parse_packet` and `Assembly` |
| Network | IPv4 loopback only; no OTA transmission |

Keep Orin power mode, cooling, and competing workload consistent with the earlier
speed test. The local receiver consumes CPU and memory bandwidth on the same
Orin; this is part of the test environment, not a remote MEC simulation.

Copy the updated checkout to the Orin, including `profile_local_submission.py`,
`profile_encoding.py`, `profile_encoding_speed.py`, the production Linux encoder,
and the contracts package. The new script imports helpers from the other profilers.

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

## 3. Set the bitrates, GOP, FPS, resolution, and packet size

Use Bash and keep these variables in the same shell:

```bash
GOP=30
FPS=30
WIDTH=1280
HEIGHT=720
MTU=1200
BITRATES=(500000 1000000 1500000 2000000 4000000)
BITRATE_ARGS=()
for bitrate in "${BITRATES[@]}"; do
  BITRATE_ARGS+=(--bitrate "$bitrate")
done
```

For a 10 FPS test, use `FPS=10` and `GOP=10`. All source frames are retained and
retimed; a 1200-frame video takes about 120 seconds at 10 FPS. Each bitrate/video
pair starts a fresh encoder, sender socket, and receiver process.

## 4. Run the local submission profile

```bash
cd ~/mec-offload-ad/multi-ue
PROFILE_DIR="runs/profiling/video-submission-linux-${FPS}fps-${GOP}gop-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PROFILE_DIR"

docker run --rm \
  --network none \
  --entrypoint python3 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e PYTHONPATH=/multi-ue/contracts/python:/multi-ue/ue/linux/src \
  -v "$PWD:/multi-ue" \
  -w /multi-ue \
  "$UE_IMAGE" \
  tools/profile_local_submission.py \
    --video-dir datasets/profile-videos \
    "${BITRATE_ARGS[@]}" \
    --width "$WIDTH" --height "$HEIGHT" \
    --fps "$FPS" --gop-frames "$GOP" \
    --mtu "$MTU" --expected-videos 8 \
    --output "$PROFILE_DIR"
```

The output directory must be empty or new. The eight-video/five-bitrate test
requires approximately 27 minutes at 30 FPS or 80 minutes at 10 FPS for these
approximately 1200-frame videos, plus setup and any delays.

Optional arguments:

- `--receive-buffer-bytes`: requested socket receive buffer; default 4194304.
  The kernel may cap or adjust it. Actual receive and send buffer sizes appear
  in `summary.csv`; the script does not change host kernel settings.
- `--drain-timeout`: maximum receiver drain time after sending finishes;
  default 2 seconds, maximum 60. The receiver can finish sooner if all submitted
  packets have arrived. This wait is excluded from sender timing.
- `--expected-videos`: default 8; explicitly change it for a smaller diagnostic set.

## 5. Review timing and receipt results

The script produces its own reports. Do not run the bitrate validator on these files.

- `profile.json`: conditions, input/code hashes, environment, status, and final
  timing/receipt flags.
- `frame-timings.csv`: frame-level timestamps, stage durations, packet counts,
  encoded and UDP payload bytes, deadlines, and receiver verification.
- `summary.csv`: one row per video/bitrate with mean/p95/p99/max timings,
  deadline misses, verified frames, and receiver errors.

```bash
python3 - "$PROFILE_DIR" <<'PYREPORT'
import csv
import json
import sys
from pathlib import Path
p = Path(sys.argv[1])
m = json.loads((p / "profile.json").read_text())
print("Status:", m["status"])
print("All deadlines met:", m.get("all_streams_meet_frame_deadlines"))
print("All receipts verified:", m.get("all_receipts_verified"))
with (p / "summary.csv").open() as stream:
    for r in csv.DictReader(stream):
        print(Path(r["video"]).name, "bps=" + r["bitrate_bps"],
              "pipeline_p99_ms=" + r["processing_p99_ms"],
              "packetization_p99_ms=" + r["packetization_p99_ms"],
              "socket_p99_ms=" + r["socket_submission_p99_ms"],
              "misses=" + r["deadline_misses"] + "/" + r["frames"],
              "verified=" + r["verified_frames"] + "/" + r["frames"],
              "receiver_errors=" + r["receiver_errors"])
PYREPORT
```

| Timing field | What it measures |
|---|---|
| `decode_ns` | Source read and decoding |
| `resize_ns` | OpenCV resize |
| `encoder_call_ns` | Production encoder call through returned H.264 NAL units |
| `descriptor_ns` | IDR validation, byte counting, and descriptor construction |
| `packetization_ns` | Production packetizer, including constructing the complete packet list |
| `socket_submission_ns` | First send-call start through last send-call return |
| `processing_ns` | Source read start through last send return, excluding pacing wait |
| `release_to_submission_ns` | Scheduled release through last send return |
| `deadline_lateness_ns` | Time beyond the next frame's release; zero if on time |

Timestamps are monotonic nanoseconds relative to each stream's start; summary
times are milliseconds. Setup is separate. The deadline is 33.33 ms per frame
at 30 FPS or 100 ms at 10 FPS. Inspect first-frame costs separately from sustained
behavior, but no frames are silently excluded from summary statistics.

The sender keeps timing rows in memory until a video finishes. Packet hashing
for receipt verification happens after each frame's sends, outside its measured
processing interval; it may delay the next frame and is therefore still visible
in schedule lateness. Receiver processing adds local workload too.

Receipt verification requires matching packet count, UDP payload byte count,
production frame assembly completion, and SHA-256 of the ordered datagrams for
each frame. It detects missing, duplicated, reordered, or changed datagrams as
unverified. Completed assemblies release their payload memory. No video decoding
or inference is performed by the receiver.

`submitted_udp_bytes` includes RTP headers and descriptor extensions; it excludes
UDP/IP headers. `encoded_bytes` counts the canonical H.264 access unit. Receiver
first/last timestamps are userspace receive times, not kernel or radio arrival times.

A `complete` status means all measurements finished. Timing misses and receipt
failures are reported independently and do not cause an early stop. Require both
`all_streams_meet_frame_deadlines` and `all_receipts_verified` for a strict test
pass; inspect the detailed results to distinguish startup misses from sustained
failure. Exceptions instead mark the run failed/cancelled; an interrupted stream
may have no saved rows, while earlier completed streams remain available.

This tests the production encoding and packetization components with real local
socket calls. It does not run the entire UE controller or reproduce its control
messages, radio workload, or MEC feedback. Successful socket submission is not
proof of OTA delivery. Compare with the isolated encoder speed profile, then test
the real radio path separately when ready. Neither test changes runtime configuration.

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

Keep those five files together. Compare the observed frame counts with the earlier
profiles too: a failed OpenCV read ends a video, and equal counts across bitrates
alone cannot prove that the entire original source decoded successfully.
