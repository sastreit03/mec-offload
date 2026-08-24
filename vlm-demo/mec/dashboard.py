from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse


router = APIRouter()


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MEC Video VLM Demo</title>
<style>
body { font-family: system-ui, sans-serif; margin:24px; background:#111; color:#eee; }
.grid { display:grid; grid-template-columns:minmax(560px,2fr) minmax(360px,1fr); gap:20px; }
.card { background:#1b1b1b; border:1px solid #333; border-radius:12px; padding:16px; }
img { width:100%; background:#000; aspect-ratio:16/9; object-fit:contain; border-radius:8px; }
.metrics { display:grid; grid-template-columns:1.2fr 1fr; gap:6px 14px; font-variant-numeric:tabular-nums; }
pre { background:#090909; border-radius:8px; padding:12px; overflow:auto; white-space:pre-wrap; }
button { margin-top:12px; margin-right:6px; padding:9px 14px; border:0; border-radius:7px; cursor:pointer; }
.ok { color:#7ee787; } .bad { color:#ff7b72; } .warn { color:#d29922; }
h2,h3 { margin-top:0; } small { color:#999; }
</style>
</head>
<body>
<h2>MEC — RTP/H.264 Receiver + VLM</h2>
<div class="grid">
  <div class="card">
    <h3>Actually received and decoded video</h3>
    <img src="/preview.mjpg" alt="Waiting for RTP/H.264 video">
    <p><small>This is generated from frames decoded at the MEC after UDP/RTP/H.264 transport through the 5G user plane.</small></p>
    <div class="metrics">
      <div>Receiver</div><div id="receiver">-</div>
      <div>Decoded FPS</div><div id="fps">-</div>
      <div>RTP bitrate</div><div id="bitrate">-</div>
      <div>RTP packets/s</div><div id="pps">-</div>
      <div>Estimated RTP loss</div><div id="loss">-</div>
      <div>RTP SSRC</div><div id="ssrc">-</div>
      <div>Frame buffer</div><div id="buffer">-</div>
      <div>UE control channel</div><div id="control">-</div>
    </div>
    <button onclick="post('/api/receiver/restart')">Restart receiver</button>
  </div>

  <div>
    <div class="card">
      <h3>VLM</h3>
      <div class="metrics">
        <div>Status</div><div id="model_status">-</div>
        <div>Model</div><div id="model_id">-</div>
        <div>Inference</div><div id="infer_running">-</div>
        <div>Last inference</div><div id="infer_ms">-</div>
        <div>Frames used</div><div id="infer_frames">-</div>
        <div>Inference count</div><div id="infer_count">-</div>
      </div>
      <button onclick="post('/api/infer-now')">Infer now</button>
      <h3 style="margin-top:18px">Current UE question</h3>
      <pre id="question">No question received.</pre>
      <h3>Latest result sent to UE</h3>
      <pre id="result">No result yet.</pre>
    </div>

    <div class="card" style="margin-top:20px">
      <h3>UE stream configuration</h3>
      <pre id="stream_cfg">Waiting for UE.</pre>
      <h3>Errors</h3>
      <pre id="errors">None.</pre>
    </div>
  </div>
</div>
<script>
const el = (id) => document.getElementById(id);
async function post(url) {
  const r = await fetch(url, {method:'POST'});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}
async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    const r = s.state.receiver, p = s.state.rtp, c = s.state.control, v = s.state.vlm, b=s.state.frame_buffer;
    el('receiver').innerHTML = r.running ? '<span class="ok">LISTENING</span>' : '<span class="bad">STOPPED</span>';
    el('fps').textContent = r.decoded_fps;
    el('bitrate').textContent = p.bitrate_kbps + ' kbps';
    el('pps').textContent = p.packets_per_s;
    el('loss').textContent = p.packets_lost_estimate + ' (' + p.loss_percent_estimate + '%)';
    el('ssrc').textContent = p.ssrc ?? '-';
    el('buffer').textContent = b.frames + ' frames / ' + b.span_s + ' s';
    el('control').innerHTML = c.connected ? '<span class="ok">CONNECTED ' + (c.peer || '') + '</span>' : '<span class="bad">DISCONNECTED</span>';
    el('model_status').innerHTML = v.status === 'ready' ? '<span class="ok">READY</span>' : (v.status === 'error' ? '<span class="bad">ERROR</span>' : '<span class="warn">' + v.status.toUpperCase() + '</span>');
    el('model_id').textContent = v.model_id || '-';
    el('infer_running').textContent = v.inference_running ? 'RUNNING' : 'idle';
    el('infer_ms').textContent = v.last_inference_ms == null ? '-' : v.last_inference_ms + ' ms';
    el('infer_frames').textContent = v.last_inference_frame_count;
    el('infer_count').textContent = v.inference_count;
    el('question').textContent = c.active_question ? JSON.stringify(c.active_question, null, 2) : 'No question received.';
    el('result').textContent = c.latest_result ? JSON.stringify(c.latest_result, null, 2) : 'No result yet.';
    el('stream_cfg').textContent = c.stream_config ? JSON.stringify(c.stream_config, null, 2) : 'Waiting for UE.';
    const errs=[];
    if (r.error) errs.push('Receiver: '+r.error);
    if (v.error) errs.push('VLM load: '+v.error);
    if (v.last_inference_error) errs.push('Inference: '+v.last_inference_error);
    if (c.last_error && c.last_error !== 'disconnected') errs.push('Control: '+c.last_error);
    el('errors').textContent = errs.length ? errs.join('\n') : 'None.';
  } catch(e) { console.error(e); }
}
setInterval(refresh, 500); refresh();
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/preview.mjpg")
async def preview(request: Request) -> StreamingResponse:
    state = request.app.state.shared

    async def generate() -> AsyncIterator[bytes]:
        last_sequence = -1
        while True:
            if await request.is_disconnected():
                return
            jpeg, sequence, _pts = state.get_preview()
            if jpeg is not None and sequence != last_sequence:
                last_sequence = sequence
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                    + jpeg
                    + b"\r\n"
                )
            await asyncio.sleep(0.03)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/api/status")
async def status(request: Request) -> dict:
    return {
        "state": request.app.state.shared.snapshot(request.app.state.frame_buffer),
        "config": request.app.state.config.model_dump(),
    }


@router.post("/api/infer-now")
async def infer_now(request: Request) -> dict:
    request.app.state.scheduler.wake()
    return {"ok": True}


@router.post("/api/receiver/restart")
async def receiver_restart(request: Request) -> dict:
    try:
        await asyncio.to_thread(request.app.state.receiver.restart)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}
