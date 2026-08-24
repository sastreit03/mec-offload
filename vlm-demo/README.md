# OAI MEC VLM demo code

This package currently contains the complete UE-side implementation under `ue/`.
The matching MEC receiver/VLM service should implement:

- UDP/RTP H.264 receiver on port 5000
- H.264 decode and decoded-frame ring buffer
- WebSocket server on `/ws/ue`
- Qwen3-VL inference worker
- structured `vlm_result` messages sent back to the UE
- MEC-side received-video browser preview

## MEC side

A complete matching MEC implementation is now included under `mec/`.
It receives RTP/H.264/UDP on port 5000, serves the UE WebSocket and MEC dashboard on TCP port 8765, buffers decoded RGB frames, and runs a sliding-window Qwen3-VL inference worker.

Start with `mec/config.yaml` using `vlm.backend: mock` to validate the communication path before enabling the actual model.
