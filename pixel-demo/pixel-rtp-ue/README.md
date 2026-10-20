# Pixel UE client for the existing MEC server — revision 2

This package replaces the **UE application only**. Keep the server attached to your
latest prompt, its configuration, Dockerfile, Compose services, and model cache.
No MEC server source or replacement server deployment is included.

Open [IMPLEMENTATION.md](IMPLEMENTATION.md) for build/install instructions and the
feature mapping from your Python UE application.

The Pixel app contains an HTML/JavaScript dashboard, native Camera2 capture,
raw-frame FPS sampling and crop/resize, hardware H.264 encoding, RFC 6184 RTP/UDP,
local decoding of the outgoing H.264, cellular routing, and WebSocket control.

It uses the server's **existing** endpoints:

- `/ws/ue`: hello, stream metadata, questions, acknowledgments, flat VLM results.
- `/preview.mjpg`: actual video decoded at the MEC, fetched as individual multipart
  JPEG frames for a capped dashboard preview.
- `/api/status`: `{state, ran_telemetry, config}`; receiver/RTP/control/VLM metrics.

All original UE dashboard controls are represented: FPS 1–30, width/height, bitrate,
GOP, crop margins, apply/restart, start/stop, question updates, results, RTP metrics,
and route information. Live camera capture replaces the file source/loop controls.
Hardware support and real-time CPU capacity determine achievable encoder settings.

Your server answers once per question ID. **Ask again** sends a new ID for the same
text. The client never asks the server to change its scheduler/configuration.

`VALIDATION.md` distinguishes successful software checks from Pixel/Spark/radio
checks still required. The archive contains source, not a prebuilt APK.
