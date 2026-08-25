# UE side: OAI 5G MEC video/VLM demo

## Media path

`prerecorded MP4 -> decode -> FPS sample -> crop -> resize -> x264 (I/P, B=0) -> RTP/H.264 -> UDP -> OAI UE tunnel -> MEC`

The local browser preview is deliberately decoded *after* x264 encoding, so it shows the image quality of the H.264 representation being transmitted. The browser preview itself is an HTTP MJPEG stream on the management/local side and isn't the 5G media transport.

## Control path

A separate WebSocket/TCP connection carries:

- `ue_hello`
- current video configuration
- updated natural-language question
- VLM result returned by MEC

Video continues while questions change.

## 1. Put the demo inside the UE image/container

Expected paths in the example configuration:

```text
/opt/ue-demo/app.py
/opt/ue-demo/config.yaml
/opt/ue-demo/video/demo.mp4
```

Either extend the OAI UE image using `Dockerfile.example`, or mount/copy this directory into an existing running UE container and install the packages from `install_deps.sh`.

The venv is created with `--system-site-packages` because GStreamer's Python GI bindings are installed by the OS package manager.

## 2. Verify OAI routing before running

Inside the UE container:

```bash
ip addr show
ip route get 192.168.72.136 from $UE_IP
```

For the sample config, the route must contain:

```text
dev oaitun_ue1
```

Change `expected_ue_interface` if your tunnel is named differently. The application intentionally fails at startup if `strict_route_check: true` and the MEC route doesn't use that interface.

Also verify the actual packets during integration:

```bash
tcpdump -ni oaitun_ue1 udp port 5000
tcpdump -ni oaitun_ue1 tcp port 8765
```

## 3. Configure

Edit `config.yaml`:

- `network.mec_ip`
- `network.control_ws_url`
- `network.expected_ue_interface`
- video source and initial codec parameters

The MEC side must listen for RTP/H.264 UDP on `video_port` and WebSocket control on `control_ws_url`.

## 4. Run

```bash
cd /opt/ue-demo
/opt/ue-demo-venv/bin/python app.py --config /opt/ue-demo/config.yaml
```

Open the UE dashboard from the Docker host/browser:

```text
http://HOST_IP:8080
```

If your UE container uses Docker bridge networking, publish `8080:8080`. If it uses host networking, the service binds directly to host port 8080.

## 5. Runtime controls

The dashboard supports:

- sampled FPS: 1-30
- width / height
- encoder target bitrate
- maximum GOP in frames
- crop values
- start / stop / restart
- question updates

Applying video settings rebuilds only the media pipeline. The WebSocket control connection remains independent.

## Notes that matter for experiments

1. `key-int-max=10` is a maximum keyframe distance, not a guarantee that every GOP is exactly 10 frames. This is appropriate for the proposed variable GOP experiment.
2. `bframes=0` removes B-frames. `tune=zerolatency` reduces x264 buffering.
3. `rtph264pay config-interval=-1` repeats SPS/PPS with every IDR for receiver recovery.
4. The RTP MTU defaults to 1200 bytes to reduce IP-fragmentation risk through the tunneled 5G user plane.
5. `udpsink sync=true` is required for a prerecorded file: otherwise a file pipeline can transmit as fast as the computer can process it rather than at media time.
6. UE dashboard RTP bitrate counts RTP packet bytes before UDP/IP/GTP/NR overhead. Use packet capture or interface counters when you need total user-plane/network overhead.
7. For formal measurements, use a sufficiently long prerecorded file and leave `loop_video=false` because seek-based looping can create a timestamp discontinuity.
