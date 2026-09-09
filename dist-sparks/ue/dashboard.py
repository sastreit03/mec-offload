from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from models import QuestionRequest, VideoUpdate


router = APIRouter()


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UE MEC Video Demo</title>
<style>
body { font-family: system-ui, sans-serif; margin: 24px; background:#111; color:#eee; }
.grid { display:grid; grid-template-columns:minmax(560px,2fr) minmax(340px,1fr); gap:20px; }
.card { background:#1b1b1b; border:1px solid #333; border-radius:12px; padding:16px; }
img { width:100%; background:#000; aspect-ratio:16/9; object-fit:contain; border-radius:8px; }
label { display:block; margin-top:9px; font-size:13px; color:#bbb; }
input { width:100%; box-sizing:border-box; background:#090909; color:#fff; border:1px solid #444; padding:9px; border-radius:6px; }
button { margin-top:12px; margin-right:6px; padding:9px 14px; border:0; border-radius:7px; cursor:pointer; }
pre { background:#090909; border-radius:8px; padding:12px; overflow:auto; white-space:pre-wrap; }
.ok { color:#7ee787; } .bad { color:#ff7b72; }
.metrics { display:grid; grid-template-columns:1fr 1fr; gap:6px 14px; font-variant-numeric:tabular-nums; }
h2,h3 { margin-top:0; }
small { color:#999; }
</style>
</head>
<body>
<h2>UE — H.264/RTP Video Transmitter</h2>
<div class="grid">
  <div class="card">
    <h3>Transmitted-video preview</h3>
    <img id="preview" src="/preview.mjpg" alt="Waiting for encoded preview">
    <p><small>This preview is decoded from the UE's encoded H.264 bitstream. The browser preview itself stays local; the 5G path carries RTP/H.264 over UDP.</small></p>
    <div class="metrics">
      <div>Stream</div><div id="running">-</div>
      <div>RTP bitrate</div><div id="bitrate">-</div>
      <div>RTP packets/s</div><div id="pps">-</div>
      <div>RTP bytes total</div><div id="bytes">-</div>
      <div>5G route</div><div id="route">-</div>
      <div>Control channel</div><div id="control">-</div>
    </div>
  </div>

  <div>
    <div class="card">
      <h3>Video configuration</h3>
      <label>Sampled FPS (1–30)<input id="fps" type="number" min="1" max="30"></label>
      <label>Width<input id="width" type="number"></label>
      <label>Height<input id="height" type="number"></label>
      <label>Encoder target bitrate (kbps)<input id="bitrate_cfg" type="number"></label>
      <label>Maximum GOP (frames)<input id="gop" type="number"></label>
      <label>Crop left<input id="crop_left" type="number" min="0"></label>
      <label>Crop right<input id="crop_right" type="number" min="0"></label>
      <label>Crop top<input id="crop_top" type="number" min="0"></label>
      <label>Crop bottom<input id="crop_bottom" type="number" min="0"></label>
      <button onclick="applyConfig()">Apply & restart media</button>
      <button onclick="post('/api/video/restart')">Restart video</button>
      <button onclick="post('/api/video/stop')">Stop</button>
      <button onclick="post('/api/video/start')">Start</button>
      <div id="cfg_error" class="bad"></div>
    </div>

    <div class="card" style="margin-top:20px">
      <h3>MEC question</h3>
      <label>Question / semantic task
        <input id="question" value="Did someone remove an object from the box?">
      </label>
      <button onclick="sendQuestion()">Update question</button>
      <h3 style="margin-top:18px">Latest MEC/VLM result</h3>
      <pre id="result">No result yet.</pre>
    </div>
  </div>
</div>

<script>
let configLoaded = false;
const el = (id) => document.getElementById(id);

async function post(url, body=null) {
  const opts = {method:'POST', headers:{'Content-Type':'application/json'}};
  if (body !== null) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

async function applyConfig() {
  document.getElementById('cfg_error').textContent = '';
  try {
    await post('/api/video/config', {
      fps:Number(el('fps').value), width:Number(el('width').value), height:Number(el('height').value),
      bitrate_kbps:Number(el('bitrate_cfg').value), gop_frames:Number(el('gop').value),
      crop_left:Number(el('crop_left').value), crop_right:Number(el('crop_right').value),
      crop_top:Number(el('crop_top').value), crop_bottom:Number(el('crop_bottom').value)
    });
  } catch(e) { document.getElementById('cfg_error').textContent = e; }
}

async function sendQuestion() {
  const text = document.getElementById('question').value;
  try { await post('/api/question', {text}); }
  catch(e) { alert(e); }
}

async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    const st = s.state.stream;
    el('running').innerHTML = st.running ? '<span class="ok">PLAYING</span>' : '<span class="bad">STOPPED</span>';
    el('bitrate').textContent = st.rtp_bitrate_kbps + ' kbps';
    el('pps').textContent = st.rtp_packets_per_s;
    el('bytes').textContent = st.rtp_bytes_total;
    el('route').textContent = s.state.route || '-';
    el('control').innerHTML = s.state.control.connected ? '<span class="ok">CONNECTED</span>' : '<span class="bad">DISCONNECTED</span>';

    const cfg = s.video_config;
    if (!configLoaded) {
      el('fps').value=cfg.fps; el('width').value=cfg.width; el('height').value=cfg.height;
      el('bitrate_cfg').value=cfg.bitrate_kbps; el('gop').value=cfg.gop_frames;
      el('crop_left').value=cfg.crop_left; el('crop_right').value=cfg.crop_right;
      el('crop_top').value=cfg.crop_top; el('crop_bottom').value=cfg.crop_bottom;
      configLoaded = true;
    }

    const q = s.state.control.active_question;
    const res = s.state.control.latest_result;
    el('result').textContent = res ? JSON.stringify(res, null, 2) : (q ? 'Waiting for MEC result for question #' + q.question_id : 'No active question.');
  } catch(e) {
    console.error(e);
  }
}
setInterval(refresh, 500);
refresh();
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
    streamer = request.app.state.streamer
    return {
        "state": request.app.state.shared.snapshot(),
        "video_config": streamer.get_config().model_dump(),
    }


@router.post("/api/question")
async def question(payload: QuestionRequest, request: Request) -> dict:
    control = request.app.state.control
    message = await control.send_question(payload.text)
    return {"ok": True, "question": message}


@router.post("/api/video/config")
async def video_config(payload: VideoUpdate, request: Request) -> dict:
    streamer = request.app.state.streamer
    control = request.app.state.control
    current = streamer.get_config()
    patch = payload.model_dump(exclude_none=True)
    try:
        new_config = current.model_copy(update=patch)
        # model_copy doesn't re-run validation in Pydantic v2, so validate explicitly.
        new_config = type(current).model_validate(new_config.model_dump())
        await asyncio.to_thread(streamer.reconfigure, new_config)
        await control.publish_stream_config(new_config)
    except Exception as exc:  # return useful error to dashboard
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "video_config": new_config.model_dump()}


@router.post("/api/video/restart")
async def restart_video(request: Request) -> dict:
    try:
        await asyncio.to_thread(request.app.state.streamer.restart)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/api/video/start")
async def start_video(request: Request) -> dict:
    try:
        await asyncio.to_thread(request.app.state.streamer.start)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/api/video/stop")
async def stop_video(request: Request) -> dict:
    await asyncio.to_thread(request.app.state.streamer.stop)
    return {"ok": True}
