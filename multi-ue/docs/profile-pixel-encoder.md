# Profile and validate the Pixel encoder

This offline test retains every decoded source frame and assigns encoder input
timestamps as `round(frame_index * 1,000,000 / FPS)` microseconds. It runs without
the MEC or RAN and drains the encoder through end of stream. It uses the production
Pixel encoder settings and YUV conversion. It does not pace transmission or test
the campaign's five-second wall-clock deadline.

Use the same eight original videos as the Linux profiler, the same bitrates,
width **1280**, and height **720**. Run once with FPS=30/GOP=30 and again with
FPS=10/GOP=10. All frames are retained at both rates, so the 10 FPS media timeline
is three times as long. Linux and Android retain their native decoding/scaling
pipelines; matching settings does not imply pixel-identical encoder inputs.

## 1. Build and install from Android Studio on the Mac

Copy the updated repository source to the Mac, including the new Java files.
Open `multi-ue/ue/android` in Android Studio. Install Android SDK 35 if prompted
and use a compatible Gradle JDK (Java 17 is supported by this project).

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
adb -s "$PIXEL_SERIAL" shell mkdir -p /sdcard/Movies/AetheredgeEncoderProfiling
adb -s "$PIXEL_SERIAL" push "$SOURCE_DIR/." /sdcard/Movies/AetheredgeEncoderProfiling/
```

Use original videos, not pre-encoded campaign variants. `profile.json` records
each source file's SHA-256 hash. Compare these with `shasum -a 256` on the Mac
or `sha256sum` on Linux to confirm identical source media.

## 3. Run the profiles

Open **Pixel Encoder Profiler**, then **Offline encoder profiling**, before
starting any streaming session. Choose the media directory and grant access.

| Setting | First run | Second run |
| --- | --- | --- |
| Bitrates (bits/s) | `500000,1000000,1500000,2000000,4000000` | Same |
| FPS | `30` | `10` |
| GOP length (frames) | `30` | `10` |
| Width | `1280` | `1280` |
| Height | `720` | `720` |

Start the profile and keep this screen in the foreground until it reports
**Complete**. Leaving the screen cancels the run. Do not run a campaign at the
same time. Each profile encodes eight videos at each of five bitrates (40 streams).
Then change FPS and GOP and start the second profile.

The app displays the output directory. Each run gets its own directory:

```text
/sdcard/Android/data/edu.clemson.mecrtp.profiling/files/profiling/
  encoding-pixel-<fps>fps-<gop>gop-<date>-<unique suffix>/
```

## 4. Retrieve and validate

Pull the results onto the Mac, then copy the run directories to
`multi-ue/runs/profiling/` on the PC if validation will run there:

```bash
adb -s "$PIXEL_SERIAL" pull \
  /sdcard/Android/data/edu.clemson.mecrtp.profiling/files/profiling/ \
  ./pixel-encoder-results/
```

From `multi-ue` on the machine containing the results, use Bash:

```bash
PROFILE_DIR='runs/profiling/<actual run directory>'
FPS=30
GOP_FRAMES=30
WIDTH=1280
HEIGHT=720
BITRATES=(500000 1000000 1500000 2000000 4000000)
BITRATE_ARGS=()
for bitrate in "${BITRATES[@]}"; do
  BITRATE_ARGS+=(--bitrate "$bitrate")
done
python3 tools/profile_encoding_validation.py \
  --profile-dir "$PROFILE_DIR" "${BITRATE_ARGS[@]}" \
  --fps "$FPS" --gop-frames "$GOP_FRAMES" \
  --width "$WIDTH" --height "$HEIGHT" --expected-videos 8
```

Repeat with the second directory and FPS=10/GOP_FRAMES=10.

## 5. Interpret the evidence

The app writes:

- `profile.json`: settings, source hashes, device identity, and completion status.
- `frame-sizes.csv`: one row per output frame, including retimed/source timestamps,
  canonical encoded bytes, raw codec output bytes, and IDR (`I`) versus non-IDR (`P`).
  `encode_elapsed_ns` is elapsed wall time since that stream's initialization.
- `streams.jsonl`: codec names/formats and submitted/output frame counts, with
  evidence of encoder end-of-stream for every completed video/bitrate pair.

The validator checks completion, timestamps, contiguous frame IDs, equal frame
counts across bitrates, requested GOP boundary IDRs, and full encoder drain. Compare
frame counts per source video with the Linux results too; the validator does not
automatically compare two runs or verify hashes against Linux files.

It produces:

- `bitrate_comparison.csv`: bytes, frames, produced bitrate, and produced/requested
  ratio, per video and aggregated across videos at each bitrate.
- `bitrate_windows.csv`: the same rate measurements in non-overlapping five-second
  **media-time** windows; a shorter final window is marked partial.
- `keyframe_summary.csv`: IDR positions/counts and extra IDRs between requested
  GOP boundaries, useful for detecting unexpectedly frequent keyframes.
- `summary.csv`: I/P size statistics.

Produced bitrate is `8 * encoded_bytes / (frames / FPS)`. The canonical byte count
matches production H.264 payload accounting, including four-byte start codes and
SPS/PPS repeated on IDRs; it excludes RTP/IP/radio overhead. Raw codec output bytes
are also retained separately. No bitrate-control mode is explicitly forced because
the production app leaves it at the encoder default. A requested bitrate is a
target, and passing structural validation does not establish that the ratio is
close to one: inspect the measurements before deciding on campaign settings.

Local source checks cannot establish hardware behavior. The Mac APK build and
the first completed Pixel run are still required to validate the device pipeline.
