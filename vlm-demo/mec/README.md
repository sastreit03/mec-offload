# MEC side: RTP/H.264 receiver + Qwen3-VL

This directory matches the UE implementation in `../ue/`.

## Data paths

Video path:

```
UE x264enc -> RTP/H.264 -> UDP/5000 -> OAI 5G -> MEC
MEC UDP -> rtpjitterbuffer -> rtph264depay -> H.264 decoder -> RGB frame buffer
```

Control path:

```
UE WebSocket client -> ws://MEC_IP:8765/ws/ue
  question / stream_config  ---> MEC
  <--- vlm_result
```

The same FastAPI process serves:

- MEC dashboard: `http://MEC_IP:8765/`
- UE control socket: `ws://MEC_IP:8765/ws/ue`

This is deliberate: the UE package is already configured for TCP port 8765.

## Important implementation choices

1. The VLM never runs inside a GStreamer callback. The callback only copies the decoded RGB frame into a bounded ring buffer.
2. VLM inference runs in a worker thread scheduled by asyncio every `inference_interval_s`.
3. A new UE question can clear the ring buffer. This makes the question apply to frames received after it was submitted.
4. If the question changes while an old inference is running, the old result is discarded.
5. The VLM is given a chronological multi-image sequence, not one still image. This is required for semantic actions such as removing an item from a box.
6. RTP metrics are measured after the jitter buffer. Packet-loss numbers are estimates based on RTP sequence gaps.

## 1. System GStreamer packages

On Debian/Ubuntu-based environments:

```bash
sudo ./install_system_deps.sh
```

If the MEC runs inside a container, install those packages inside that container.

## 2. Python environment

Install the core application dependencies:

```bash
python3 -m pip install -r requirements.txt
```

For real Qwen3-VL, first make sure the Python environment has a CUDA-enabled PyTorch build suitable for DGX Spark. Do **not** blindly replace NVIDIA's PyTorch build with a generic CPU wheel.

Then:

```bash
python3 -m pip install -r requirements-vlm.txt
```

Check the environment:

```bash
python3 check_environment.py
```

## 3. First end-to-end test without a model

Edit `config.yaml`:

```yaml
vlm:
  backend: mock
```

Run:

```bash
python3 app.py --config config.yaml
```

Open:

```
http://MEC_IP:8765/
```

Start the UE. You should see:

- the received video on the MEC dashboard;
- RTP bitrate and decoded FPS;
- `CONNECTED` for the UE WebSocket;
- the UE's current question;
- mock results returning to the UE dashboard.

Only after all of that works should you enable the real model.

## 4. Enable Qwen3-VL

Edit:

```yaml
vlm:
  backend: transformers
  model_id: Qwen/Qwen3-VL-4B-Instruct
```

Restart the MEC app. The dashboard shows `LOADING` and then `READY` when model initialization completes.

The model is loaded once and reused for every inference.

## 5. VLM window

Default:

```yaml
inference_interval_s: 2.0
window_seconds: 3.0
max_frames: 10
min_frames: 3
```

At a UE stream rate of 5 FPS, the MEC can receive about 15 frames in a 3-second window. It uniformly selects at most 10 so the VLM sees the start, middle, and end of the temporal window without processing every frame.

## 6. Networking/firewall

The MEC must accept:

- UDP 5000 from the UE for RTP/H.264;
- TCP 8765 from the UE for WebSocket control;
- TCP 8765 from the machine used to view the MEC browser dashboard.

If a host firewall is enabled, open only the required trusted/private interfaces/subnets.

To verify reception:

```bash
sudo tcpdump -ni any udp port 5000
sudo tcpdump -ni any tcp port 8765
```

The UE should independently verify that its route to `MEC_IP` uses `oaitun_ue1`.

## 7. Files

- `app.py`: FastAPI lifecycle and process entry point.
- `receiver.py`: GStreamer RTP/H.264 receiver and decoder.
- `models.py`: configuration, shared state, decoded-frame ring buffer.
- `control.py`: WebSocket server matching the UE protocol.
- `vlm.py`: Qwen3-VL load/inference and JSON parsing.
- `scheduler.py`: periodic sliding-window inference.
- `dashboard.py`: real-time received-video dashboard.
- `config.yaml`: all experiment parameters.
- `check_environment.py`: dependency/GPU/GStreamer check.

## Model note

The current implementation uses ordered images rather than encoding a second temporary video clip for the VLM. This is intentional: the H.264 stream is already decoded for display and buffering, and Qwen3-VL supports multiple image inputs. The prompt explicitly tells the model the chronological frame order and relative timestamps.
