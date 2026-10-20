# Pixel RTP client with the MEC server unchanged

## 1. Use this revised package

This revision supersedes the first package. Extract its `pixel-rtp-ue/` folder.
Keep running your existing **`mec-vlm` server** with the code attached to your latest
prompt. Do not deploy the replacement MEC server from the first package.

This archive contains only the new UE app, documentation, and tests. The uploaded
server files were read for compatibility checks and were not edited.

The app is an installed Android application with a web dashboard. Camera/codec/RTP
and network requests use native Android code; dashboard controls/results use bundled
HTML and JavaScript. There is no WebRTC. Opening the bundled HTML in Chrome alone
does not provide a native camera/RTP bridge.

## 2. Existing server interfaces used by the client

| Server function | Existing interface | Pixel behavior |
|---|---|---|
| Receive H.264 video | UDP 5000, PT 96, 90 kHz RTP clock | Send camera H.264 using single-NAL/FU-A RTP |
| UE control | `ws://192.168.72.136:8765/ws/ue` | Send hello, stream configuration, and questions |
| Return VLM answers | Same WebSocket | Read top-level `answer`, `details`, `confidence`, `event_detected`, `frame_count`, and timing |
| Decoded MEC preview | `GET /preview.mjpg` | Read first Content-Length JPEG, close response, repeat up to 2 FPS |
| MEC status | `GET /api/status` | Parse the actual `{state, ran_telemetry, config}` envelope |

The server has no `/api/frame.jpg` endpoint. That endpoint from the first package is
not used here. The client does not expect nested `result.answer` or a `result_id`:
your actual scheduler emits flat result fields.

The client reads `state.receiver`, `state.rtp`, `state.control`, `state.vlm`,
`state.frame_buffer`, and the existing `ran_telemetry` payload. Displaying telemetry
does not add a new gRPC client to the Pixel; it reads the MEC's existing cached data.

Your scheduler records `_answered_question_id` and skips subsequent inference for
that ID. Consequently **Submit question** and **Ask again** always create new integer
IDs. Reconnection resends the latest existing ID/text, retaining the current task.
Clicking the server's existing “Infer now” does not bypass that answered-ID guard;
this client does not pretend that it does.

## 3. Mapping the old UE functions

The UE files inspected for this revision include your UE `app(4).py`, `control(4).py`,
`dashboard(3).py`, `models(3).py`, `streamer(1).py`, `network.py`, configuration, and
README. This table maps their application behavior into the Pixel implementation.

| Python UE behavior | Pixel equivalent |
|---|---|
| Prerecorded source / seek-loop | Live rear camera; no file path or loop toggle |
| `videorate` sampled FPS 1–30 | Sample raw camera frames before encoding; defaults to 5 FPS |
| Width / height | Numeric, even output dimensions; defaults to 640×360 |
| Encoder bitrate | Numeric target in kbps, defaults to 400; applying rebuilds media |
| Maximum GOP control | GOP target in frames, defaults to 10; see hardware semantics below |
| Four crop margins | Raw capture-image pixel margins before resizing |
| Apply & restart media | Rebuild capture/encoder/local decoder/UDP sender, preserving control |
| Start / stop / restart | Buttons affect UE media only |
| Encoded-then-decoded local preview | Native decoder consumes the actual outgoing H.264 bytes |
| RTP bitrate, packets/s, byte count | Sender counters plus complete sender diagnostics |
| Strict UE route binding | Explicit cellular network selection/binding and displayed LinkProperties |
| WebSocket auto-reconnect | Retry after socket failure; retain latest question/configuration |
| New question clears visible result | Immediate UE UI reset with a new question ID |
| Ignore result for old question | Reject unmatched question IDs in the dashboard |
| Latest result JSON | Complete returned result, readable answer/details, timing/frame metrics |
| Local dashboard HTTP server | Bundled dashboard inside WebView; no separate UE HTTP server needed |

The source's optional local JPEG quality setting is replaced by a native decoded
H.264 preview, which does not generate a local MJPEG stream. The server controls the
quality of **its own** MJPEG endpoint; the Pixel app does not change that setting.

Host/container verification, image-building, dependency-installation, packet-capture,
and OAI Compose patch scripts remain host administration tools. They are not ported
into a handset dashboard. The old software-UE container patches and prerecorded-video
launcher are not needed to run this Pixel UE app.

## 4. Network prerequisites

The Pixel must already attach over the air to your OAI gNB and establish a working
data PDU session. The gNB's B210 supplies the RF frontend; the Pixel uses its own
modem and antennas. This app cannot resolve modem/SIM/PLMN/APN attachment failures.

Use the assigned handset PDU IP, not the former software UE's assumed `12.1.1.2`.
For initial checks, disable Wi-Fi and make the OAI SIM the default mobile-data SIM.

Your provided server Compose file gives `mec-vlm` the N6 container address
**192.168.72.136** on **oai-traffic-net**. Its `mec-route-init` service adds a return
route for **12.1.1.0/24 via 192.168.72.134**. That works only if the handset PDU address
is in that subnet and the corresponding UPF path is operational.

The server Compose publishes **TCP 8765**, but does not publish **UDP 5000** to the
host. Enter **192.168.72.136** as the Pixel's destination when using your direct N6
container route. Entering a Spark host IP for both protocols would not automatically
deliver UDP video to this container. No port-publication change is needed for the
existing direct container-IP path.

The app requests a CELLULAR network advertising INTERNET capability and binds its
process before opening sockets. It does not demand Android's VALIDATED capability,
so lack of public internet is not itself a rejection criterion. OkHttp proxy use is
disabled for the private 5G path. If the cellular network cannot reach the MEC, fix
the underlying OAI/PDU routing rather than expecting Wi-Fi fallback.

For the two-Spark split, leave the existing MEC service on the core/MEC Spark; the
handset reaches it through gNB -> UPF -> N6. The client has no dependence on the
gNB and MEC being on the same host.

## 5. Build and install the UE app

On a development laptop/desktop:

1. Install Android Studio and SDK Platform **35**, plus Platform Tools and the
   required SDK build tools.
2. Open **`pixel-rtp-ue/android/`** as an existing project.
3. Use the included Gradle **8.9** wrapper and select **JDK 17** as the Gradle JDK.
   The project pins Android Gradle Plugin **8.7.3** and OkHttp **4.12.0**.
4. Let Gradle sync obtain dependencies over the development computer's internet
   connection. The wrapper validates the official Gradle distribution checksum.
5. Enable Developer options and USB debugging on the Pixel. Connect USB and accept
   the development computer's debugging authorization.
6. Select the Pixel in Android Studio and click **Run** to build/install the APK.

CLI alternative with JDK 17, Android SDK 35, and Platform Tools installed:

```bash
cd pixel-rtp-ue/android
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n edu.clemson.mecrtp/.MainActivity
```

On Windows use `gradlew.bat assembleDebug`. The generated debug APK is suitable for
testbed installation. No prebuilt APK is included in this source archive.

There is no browser-camera HTTPS certificate setup: Camera2 uses Android camera
permission. The WebView serves only bundled HTML at a fixed local origin; native
code performs all network operations. It blocks navigation and external page loads
into the privileged bridge.

## 6. First run with your existing server

Do not rebuild/reconfigure the MEC for this application. Your supplied server config
already enables `transformers` with `Qwen/Qwen3-VL-4B-Instruct`; keep that as is.
Its offline model/cache settings also remain unchanged.

1. Confirm that the existing `mec-vlm` container is running and that the handset has
   OAI attachment and mobile data.
2. Open **Pixel RTP MEC** on the Pixel. It uses landscape orientation.
3. Enter MEC **192.168.72.136**, control TCP **8765**, and WebSocket path **/ws/ue**.
4. Tap **Connect**. Expand the route information to see the selected cellular
   interface, addresses, and route data.
5. Leave the initial media settings at **5 FPS**, **640×360**, **400 kbps**,
   **GOP 10**, zero crop, **MTU 1200**, and UDP **5000**.
6. Tap **Start**, grant camera permission, and tap Start again if necessary. If the
   permission dialog caused a disconnect, reconnect first.
7. The left pane should show the outgoing H.264 decoded **on the Pixel**. The web
   dashboard's “Decoded at the MEC” image should show frames received/decoded at
   the server. These are separate previews, not duplicates of a raw camera view.
8. Enter a question and submit. Expect `question_ack`, then one `vlm_result` with
   the real server's answer when enough frames and model readiness permit inference.
9. Tap **Ask again** for another evaluation of the same text with a new ID.

The model/status diagnostics come from the unchanged server, including load errors,
inference errors, frame-buffer statistics, estimated RTP loss, SSRC, and RAN state.
No extra server readiness or JPEG routes are required.

## 7. Adjust the stream

**Apply & restart** validates settings then rebuilds only the UE media path. This
matches the old UE's media-reconfiguration behavior. The WebSocket remains open and
the new configuration is published only after media starts successfully.

**Restart video** uses the last successfully applied configuration. **Stop** leaves
control available. **Start** uses the settings currently visible in the fields.
**Disconnect** stops media and releases the cellular network request. Leaving the
foreground app also disconnects/stops media; background capture is not implemented.

The native capture requests a camera AE range supporting 30 FPS. It selects 1280×720
YUV capture where available, otherwise 640×480, and reports the selected capture
dimensions. It samples that raw stream to the requested output FPS **before** AVC
encoding. It does not drop already encoded P-frames to reduce FPS.

Crop margins refer to that capture image, not the resized output. Margins must be
nonnegative even integers and leave a nonempty region; output dimensions must also
be even. Raw YUV planes are cropped/resized with nearest-neighbor sampling while
respecting row/pixel strides. The resulting output can be stretched if its aspect
ratio differs from the cropped input, as with the original resize pipeline.

Dimension/bitrate controls retain the old UE validation ranges. A particular encoder
may reject some combinations within those ranges; the error is shown rather than
silently falling back. CPU YUV processing and local decoding also limit achieved
FPS at larger resolutions. Use measured encoded FPS to evaluate actual performance,
and start with the original 640×360 / 5 FPS configuration.

The hardware codec replaces x264. Baseline AVC is used to avoid B-frames; the existing
MEC `rtph264depay ! h264parse ! avdec_h264` path accepts that H.264 profile. SPS/PPS
are repeated at IDR frames. The sender honors the configured RTP MTU.

### GOP semantics for experiments

The old UE used x264 `key-int-max`. Android's hardware codec exposes a time-based
I-frame interval and sync-frame requests. This client sets the interval from
`gop_frames / fps` and requests a sync frame at each configured input-frame interval.
That is an **encoder target**, not a proven hard x264-style maximum on every device.
The sender diagnostics report `max_observed_gop` from encoded keyframe flags.
Measure IDR distances in captured RTP/H.264 before using this setting for formal
GOP comparisons. A strict x264-equivalent bound would require additional codec
enforcement or a software x264 integration on the UE; it does not require a server
change.

## 8. Control reconnect and result behavior

If the WebSocket drops while the cellular network is still present, RTP media
continues. The client retries control after two seconds and resends the latest
stream metadata/question. HTTP status can recover the server's stored latest result
if the result arrived while the WebSocket was unavailable.

If the cellular network itself is lost, the app stops media and reports the failure.
Reconnect explicitly once the PDU network returns. The media operation is kept
independent of WebSocket reconnect; the question ID changes only for a new submission
or **Ask again**, not merely because the socket reconnects.

The UI shows top-level VLM answer/details, event status, confidence, inference time,
frame count, complete JSON, and up to five previous answers. Model confidence is a
self-assessment, not calibrated probability. `inference_ms` is server model runtime;
RTP timestamps and question timestamps do not constitute a synchronized handset-to-
MEC one-way latency measurement.

## 9. Preview traffic and measurements

The MEC's existing `/preview.mjpg` is a continuous HTTP multipart stream. This client
reads **one JPEG per request**, closes that response, and waits before asking again.
It therefore does not consume a full-FPS downlink MJPEG stream merely to display a
2-FPS preview. Actual refresh rate is at most 2 FPS and can be lower under load.

Uncheck **Return decoded preview** during uplink-heavy experiments. That removes
Pixel requests to `/preview.mjpg`; HTTP status and small WebSocket control/results
remain. The locally decoded H.264 pane does not generate network traffic.

The client uses the server's changing `preview_sequence` to detect a stopped/stale
preview, because the existing MJPEG endpoint can return a cached last image. Server
FPS/rate values can also remain cached after the stream stops; inspect packet/frame
sequence progress as well as the displayed rates.

RTP sender bitrate counts RTP packet bytes, excluding UDP/IP/GTP/radio overhead.
Estimated loss shown by the server is the server's own sequence-gap metric. Do not
treat those counters as exact radio throughput/loss without validating the measurement
boundary. This sender has no RTCP/NACK/RTX/FEC/SRTP/audio/congestion controller.

## 10. Troubleshoot without editing the server

On the core/MEC Spark:

```bash
docker logs --tail 100 mec-vlm
sudo tcpdump -ni any 'udp port 5000 or tcp port 8765'
```

On N3, packets are encapsulated in UDP **2152**; inspect their inner traffic in
Wireshark. Use N6/MEC capture to inspect native UDP 5000 RTP directly.

| Symptom | Likely boundary |
|---|---|
| No cellular network | SIM/PLMN/APN/subscriber/PDU setup, data SIM, mobile data |
| Control cannot connect | Destination IP, TCP 8765, private route and return route |
| Local decoded video but no UDP reception | RTP port, UPF/N6 route/firewall; do not substitute an unpublished host UDP port |
| RTP arrives but MEC decoded frames do not advance | PT 96, SPS/PPS/IDR reception, loss, GStreamer logs |
| MEC preview missing | Existing `/preview.mjpg` route, JPEG multipart response, preview toggle, sequence progress |
| Camera/encoder settings rejected | Try 640×360 / 5 FPS / no crop; inspect reported hardware error |
| Answer received only once | Expected behavior: submit again with a new question ID |
| No real VLM answer | Existing model readiness/error, fresh frames since question, server inference errors |
| Control reconnecting while video plays | Expected separation of RTP from control; latest state is resent after recovery |

The existing server's `/api/receiver/restart` can be invoked from its own dashboard
for receiver recovery. This changes runtime state, not source code. It is not an
automatic dependency of the Pixel client.

## 11. Software validation and remaining hardware work

See `VALIDATION.md`. Tests use exact copies of the uploaded server modules as fixtures
and never replace/edit the originals. To run the server contract test yourself:

```bash
python3 -m pip install fastapi pydantic PyYAML numpy Pillow httpx
python3 pixel-rtp-ue/tests/test_server_contract.py --server-dir /path/to/mec_app
```

For native utility tests with a JDK installed:

```bash
mkdir -p /tmp/pixel-rtp-tests
javac -d /tmp/pixel-rtp-tests \
  pixel-rtp-ue/android/app/src/main/java/edu/clemson/mecrtp/H264Rtp.java \
  pixel-rtp-ue/android/app/src/main/java/edu/clemson/mecrtp/MjpegReader.java \
  pixel-rtp-ue/android/app/src/main/java/edu/clemson/mecrtp/YuvScaler.java \
  pixel-rtp-ue/tests/RtpProtocolTest.java pixel-rtp-ue/tests/MediaUtilitiesTest.java
java -ea -cp /tmp/pixel-rtp-tests RtpProtocolTest
java -ea -cp /tmp/pixel-rtp-tests MediaUtilitiesTest
```

Build/install and test on the Pixel to confirm flexible-YUV codec input, camera FPS,
crop orientation, local decoder rendering, hardware GOP behavior, and achievable
performance. The 5G and GPU path must be tested on your actual hardware. Keep the
app visible while streaming, and use the private testbed: no app authentication or
media encryption has been added.
