# Profile Pixel video submission over local UDP

This test measures the Pixel's paced raw-image preparation, encoding, production
RTP packetization, and UDP socket submission. A receiver thread on the same Pixel
checks packet counts, bytes, frame completeness, and ordered packet hashes. The
destination is fixed to IPv4 loopback (`127.0.0.1`); no radio, MEC, or external
receiver is required.

It adds a separate **Start paced local UDP profile** button to the existing
profiling screen. The existing bitrate-only profile remains available. This test
requests CBR explicitly and fails if the selected encoder does not advertise CBR
support. Production streaming bitrate-mode settings are unchanged.

## Measurement conditions

Use the same eight original videos and five bitrates as the earlier profiles.
Keep charging, device temperature, and competing workload comparable between runs.
The receiver thread consumes resources on the same phone; results include that
local workload and do not predict performance with the radio active.

All source frames are retained. The decoder receives the source's original
presentation timestamps so its normal reordering works. Encoder input timestamps
are independently assigned as `round(frame_index * 1,000,000 / FPS)` microseconds.
Pacing uses `round(frame_index * 1,000,000,000 / FPS)` nanoseconds from a monotonic
stream origin. Thus a 1200-frame source represents 40 seconds at 30 FPS or 120
seconds at 10 FPS; lowering FPS does not select every third source frame.

## 1. Build and install from Android Studio on the Mac

Copy the updated repository source to the Mac, including the new Java files.
Open `multi-ue/ue/android` in Android Studio. Install Android SDK 35 if prompted
and use a compatible Gradle JDK (Java 17 is supported by this project).

The app now builds a native CPU YUV library. In **Tools → SDK Manager → SDK
Tools**, enable **Show Package Details** and install **NDK (Side by side)
27.0.12077973** and **CMake 3.22.1**, then sync Gradle. Copy the updated
`app/build.gradle`, the complete `app/src/main/cpp/` directory, and the Java
sources to the Mac; copying Java files alone is no longer sufficient. Android
Studio packages `libmec_yuv.so` in the APK. No GPU or external native dependency
is used.


For a separate installation alongside the campaign app, add this line to the
project's local `gradle.properties` on the Mac, then sync Gradle:

```properties
encoderProfiling=true
```

Keep that local setting out of commits. Its **presence** enables the profiling
package; setting it to `false` still enables it. Remove the line for ordinary
campaign builds.

Connect the Pixel by USB, enable USB debugging, accept the phone's authorization
prompt, select it as the deployment target, and Run the `app` configuration.
This build installs as **Pixel Encoder Profiler**, package
`edu.clemson.mecrtp.profiling`, with separate app data.

## 2. Select the original media

Use a directory containing only the eight original Linux profiling videos. The
picker reads videos directly inside the directory, not nested directories. If
the originals are already on the Pixel in such a directory, select that directory.
Otherwise, using `adb` from Android SDK Platform Tools on the Mac:

```bash
adb devices
PIXEL_SERIAL='<serial from adb devices>'
SOURCE_DIR='<directory containing the eight original videos on the Mac>'
adb -s "$PIXEL_SERIAL" shell mkdir -p /sdcard/Movies/EncoderProfiling
adb -s "$PIXEL_SERIAL" push "$SOURCE_DIR/." /sdcard/Movies/EncoderProfiling/
```

Use original videos, not pre-encoded campaign variants. `profile.json` records
each source file's SHA-256 hash. Compare these with `shasum -a 256` on the Mac
or `sha256sum` on Linux to confirm identical source media.

## 3. Run the local submission profiles

Copy all updated Java sources to your Mac before rebuilding, including:

- `LocalSubmissionProfiler.java`: paced codec processing and timing reports.
- `ProfilingLoopback.java`: local sockets, send timing, and receiver verification.
- `ProfilingTimeline.java`: frame-index-based timestamps and release times.
- `EncoderProfileActivity.java`: the additional profiling button.

Open **Pixel Encoder Profiler**, then **Offline encoder profiling**, before
starting any streaming session. Select the original media directory.

| Setting | First run | Second run |
|---|---|---|
| Bitrates (bits/s) | `500000,1000000,1500000,2000000,4000000` | Same |
| FPS | `10` | `30` |
| GOP length (frames) | `10` | `30` |
| Width | `1280` | `1280` |
| Height | `720` | `720` |

Press **Start paced local UDP profile**. Keep the screen open until completion;
leaving the screen requests cancellation. Do not run a streaming campaign at the
same time. For a shorter initial hardware smoke test, enter only one bitrate;
the app still requires the eight-video directory. You can cancel after inspecting
Logcat, but only a completed run is valid profiling evidence.

Each full run tests 40 video/bitrate pairs, initializing codecs and sockets for
each pair. Allow about 80 minutes at 10 FPS or at least 27 minutes at 30 FPS for
these videos, plus setup or processing delays. Frames are not discarded when
late: processing continues against the original schedule and records lateness.

A frame's deadline is the next frame's scheduled release. The frame meets it if
the last UDP send call returns by that time. This is separate from the campaign's
five-second transmission cutoff. Encoder EOS is fully drained before finalizing
each stream, and the receiver gets up to two seconds to drain remaining packets.

The shared encoder format still requests a one-second I-frame interval, and the
profiler explicitly requests each GOP boundary IDR. The recommended FPS/GOP pairs
agree on a one-second interval; arbitrary GOP changes may allow extra IDRs.

## 4. Pull the results

### Optional orientation diagnostic

To verify the current native conversion visually, check **Orientation
diagnostic: save 30 frames; run first video at lowest bitrate only**, then press
**Start paced local UDP profile**. The eight-video directory is still required,
but this option runs only its first alphabetical video at the lowest selected
bitrate. It processes that entire video and saves its first 30 encoded frames.
At 10 FPS, allow about two minutes for the first approximately 1200-frame video.

The sample includes SPS/PPS and starts with an IDR. It is buffered in memory and
written after the stream ends as `orientation-check.h264`; `orientation-check.json`
records the source, FPS, dimensions, and captured frame range. Capture work occurs
after timed submission but can affect later releases. `profile.json` therefore
marks `orientation_diagnostic=true` and `timing_results_for_benchmark=false`.
Uncheck the option for normal timing runs. The diagnostic uses the same corrected native rotation/conversion as normal
profiling; enabling capture does not select a different image transform.

Both app screens now support portrait and landscape device orientations. Rotation
is handled without recreating the activities, preserving active profiling work.
This changes the UI orientation only, not video pixels or encoder dimensions.
Leaving the profiling screen still cancels the run.

After pulling the result directory, extract its first frame on a machine with
FFmpeg installed:

```bash
PROFILE_DIR='runs/profiling/<actual diagnostic directory>'
ffmpeg -i "$PROFILE_DIR/orientation-check.h264" -frames:v 1 \
  "$PROFILE_DIR/pixel-first-frame.png"

# FFmpeg's default autorotation applies the original video's display rotation.
ffmpeg -i datasets/profile-videos/00cea101-06f21d5e.mov -frames:v 1 \
  "$PROFILE_DIR/source-first-frame.png"
```

Compare the two images for orientation, stretching, and cropping. The elementary
H.264 sample has no container rotation tag; inspect its pixels as encoded. The
sidecar records the intended playback FPS (30 captured frames are three seconds
at 10 FPS or one second at 30 FPS).

The app displays the output directory:

```text
/sdcard/Android/data/edu.clemson.mecrtp.profiling/files/profiling/
  video-submission-pixel-<fps>fps-<gop>gop-<date>-<unique suffix>/
```

From a machine connected to the phone:

```bash
adb devices
PIXEL_SERIAL='<your Pixel serial>'
adb -s "$PIXEL_SERIAL" pull \
  /sdcard/Android/data/edu.clemson.mecrtp.profiling/files/profiling/ \
  ./pixel-submission-results/
```

Copy the desired run directory into `multi-ue/runs/profiling/` on this PC.

## 5. Read the timing and receipt reports

The app produces its own summary. Do not run `profile_encoding_validation.py`
on these artifacts; that validator expects the bitrate-only CSV schema.

- `profile.json`: requested settings, device information, source hashes, timing
  policy, completion status, and overall deadline/receipt flags.
- `frame-timings.csv`: one row per frame, retimed and original timestamps,
  timing stages, packet/byte counts, and receipt verification.
- `summary.csv`: per-video/bitrate mean, p95, p99, and maximum stage times,
  deadline misses, verified frames, and final lateness.
- `streams.jsonl`: codec names, source/requested/accepted input/output formats,
  frame counts, encoder EOS evidence, and actual socket buffer sizes.

The recorded accepted/output bitrate mode may show `1` despite the CBR request.
On the tested Pixel, internal Codec2 logs showed `algo.bitrate-mode=1` (internal
CBR) while the Java formats reported `1` (Java VBR). Preserve these formats as
reported; do not treat the returned Java value alone as decisive evidence of the
internal rate-control mode.

```bash
PROFILE_DIR='runs/profiling/<actual video-submission-pixel directory>'
python3 - "$PROFILE_DIR" <<'PYREPORT'
import csv
import json
import sys
from pathlib import Path
p = Path(sys.argv[1])
m = json.loads((p / 'profile.json').read_text())
print('Status:', m['status'])
print('All deadlines met:', m.get('all_streams_meet_frame_deadlines'))
print('All receipts verified:', m.get('all_receipts_verified'))
with (p / 'summary.csv').open() as stream:
    for r in csv.DictReader(stream):
        print(r['video'], 'bps=' + r['bitrate_bps'],
              'copy_p99_ms=' + r['copy_p99_ms'],
              'encoder_latency_p99_ms=' + r['encoder_latency_p99_ms'],
              'rtp_non_socket_p99_ms=' + r['rtp_non_socket_p99_ms'],
              'socket_p99_ms=' + r['socket_submission_p99_ms'],
              'misses=' + r['deadline_misses'] + '/' + r['frames'],
              'verified=' + r['verified_frames'] + '/' + r['frames'])
PYREPORT
```

| Field | Meaning |
|---|---|
| `decoder_latency_ns` | Decoder input queue-call return to matching output retrieval, using original source PTS |
| `decoded_hold_ns` | Time decoded output was held before image preparation |
| `copy_ns` | Shared YUV crop/resize/copy into encoder input |
| `encoder_queue_call_ns` | Duration of the encoder input queue call |
| `encoder_latency_ns` | Encoder input queue-call return to matching encoded output retrieval |
| `output_copy_ns` | Retrieval of encoded buffer contents into a byte array and output checks |
| `rtp_total_ns` | Entire existing `H264Rtp.accessUnit()` call, including interleaved sends |
| `socket_submission_ns` | Sum of durations of all `DatagramSocket.send()` calls for the frame |
| `verification_bookkeeping_ns` | Sender packet hashing/counting inside the sink |
| `rtp_non_socket_ns` | RTP total minus socket-call and verification-bookkeeping durations |
| `processing_ns` | Raw-image preparation start through final socket-send return |
| `release_to_submission_ns` | Planned release through final socket-send return |
| `deadline_lateness_ns` | Time beyond the next planned release; zero if on time |

Decoder work is prefetched and may overlap pacing and encoding. Its measured
latency includes reorder buffering, downstream backpressure, and polling delay;
it is not pure decoder execution time and must not be added to the sender-stage
totals. Encoder latency likewise includes polling and scheduling, not just hardware
execution. One encoder input image is outstanding at a time, matching the existing
production input fence. Decoded images wait for the scheduled release before
submission. Late first frames remain in statistics; setup is reported separately.

`processing_ns` starts after a raw image is available, unlike the Linux test's
processing duration which starts before its synchronous OpenCV read. Compare
`release_to_submission_ns` and deadline results for schedule compliance, and use
the stage measurements to explain differences. Do not compare decoder latency
with Linux synchronous read duration as if they were equivalent operations.

Packetization preserves the production construct/send sequence: packets are not
prebuilt into a list. `rtp_non_socket_ns` includes NAL normalization, metadata,
packet construction, and remaining sink/call overhead; it is not an isolated
packet-construction benchmark. Sender hashing and receiver activity add profiling
workload. CSV writes happen after each stream, outside per-frame timing.

Per-frame receipt verification requires matching ordered packet hashes, packet
and byte counts, the final RTP marker, and canonical H.264 payload length. A
missing/duplicated/reordered/changed packet therefore prevents verification.
The receiver understands the production packetizer's fixed RTP layout. It does
not decode the video or run application inference. `received_udp_bytes` includes
RTP headers but excludes UDP/IP headers. Receive timestamps are userspace times.

At 10 FPS the budget is 100 ms; at 30 FPS it is 33.33 ms. `status=complete` means
the run finished, not that deadlines or receipts passed. Inspect both overall
flags and per-stream results. Exceptions mark failure/cancellation; completed
streams remain saved, but an interrupted stream may have no saved timing rows.
No measurements establish radio performance or remote MEC latency.

## 6. Preserve provenance

On the PC:

```bash
(
  cd "$PROFILE_DIR"
  sha256sum profile.json frame-timings.csv summary.csv streams.jsonl > SHA256SUMS
  sha256sum -c SHA256SUMS
)
```

Keep all five files and the APK/source revision used to build the app. Local
Java compilation and loopback tests do not replace an actual Pixel smoke test;
MediaCodec behavior and timings must be verified on the phone after rebuilding.

## Native CPU image conversion

Prerecorded streaming and both Pixel profilers now read each source track's
clockwise rotation metadata (0/90/180/270 degrees), save it, and configure the
buffer-mode decoder with rotation zero. The shared native conversion applies the
saved rotation exactly once. Screen rotation does not determine video rotation.
Camera input uses the existing zero-rotation policy.

The converter processes Y, U, and V directly from the Android image buffers,
respecting crop rectangles, buffer positions/limits, row padding, and planar or
interleaved chroma strides. It uses bulk row copies for compatible unrotated
images, tiled rotation when rotated dimensions match, and fused rotation plus
nearest-neighbor scaling otherwise. Scaling fills the requested dimensions, as
before; it does not introduce letterboxing. It does not convert through RGB or
allocate a full-frame scratch image. Scaling coordinate arrays are reused by the
codec thread. Chroma samples remain YUV420, with even crop/output dimensions
required. Unsupported geometry fails explicitly instead of corrupting buffers.

For the eight current sources, 720×1280 rotated by their individual 90° or 270°
metadata becomes 1280×720, so the normal test requires rotation/layout conversion
only. `image_conversion=native_cpu_rotate_scale_v1` identifies the new profiles;
`streams.jsonl` also records `source_rotation_degrees`. The orientation diagnostic
sidecar records the applied rotation too.

After rebuilding, inspect a new diagnostic sample before running full timing and
bitrate tests. Keep earlier results as the old conversion baseline: corrected
orientation changes encoder input, so previous bitrate measurements should not
be treated as measurements of the corrected implementation. Host correctness
checks do not establish the Pixel speedup; compare new `copy_ns` and deadline
measurements on the actual phone.
