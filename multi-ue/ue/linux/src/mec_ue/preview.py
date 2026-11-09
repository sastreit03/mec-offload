"""Local outgoing-frame preview. JPEG work never runs on the RTP sender thread."""
import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTML = b'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Linux UE preview</title><style>
body{font:16px system-ui;background:#101820;color:#e9eff4;margin:24px}
.grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:20px;align-items:start}
.card{background:#192631;border:1px solid #344451;border-radius:12px;padding:18px;min-width:0}
h2{margin-top:0}img{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px}
img[hidden]{display:none}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin-bottom:0}
small{color:#afc1ce}@media(max-width:850px){.grid{grid-template-columns:1fr}}
</style></head><body><h1>Linux UE outgoing video</h1>
<div class="grid"><section class="card"><h2>Outgoing video</h2>
<img id="video" alt="Waiting for outgoing frames">
<p><small>Frames passed to the encoder, before compression and transmission. Preview is limited to 5 FPS.</small></p>
</section><section class="card"><h2>UE and stream status</h2>
<pre id="status">Connecting...</pre></section></div><script>
const video=document.getElementById('video');
function watch(){video.removeAttribute('src');if(!document.hidden)video.src='/preview.mjpg';}
document.addEventListener('visibilitychange',watch);window.addEventListener('pagehide',()=>video.removeAttribute('src'));watch();
async function update(){try{const s=await(await fetch('/api/status',{cache:'no-store'})).json();
 document.getElementById('status').textContent=JSON.stringify(s,null,2);video.hidden=!s.active;
}catch(e){document.getElementById('status').textContent='Preview server unavailable';}setTimeout(update,1000);}update();
</script></body></html>'''


def encode_jpeg(bgr):
    import cv2
    h, w = bgr.shape[:2]
    if w > 960:
        bgr = cv2.resize(bgr, (960, round(h * 960 / w)))
    ok, data = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    return data.tobytes()


class Preview:
    def __init__(self, alias, host='127.0.0.1', port=8767, encode=encode_jpeg):
        self.alias, self.host, self.port, self.encode = alias, host, port, encode
        self.lock = threading.Condition()
        self.viewers = 0
        self.pending = self.jpeg = self.video_id = None
        self.info = {}
        self.error = None
        self.sequence = 0
        self.closed = False
        self.server = None

    def begin(self, video_id, source, config):
        with self.lock:
            self.video_id = video_id
            self.info = dict(source=source, config=dict(config))
            self.pending = self.jpeg = None
            self.error = None

    def end(self, video_id):
        with self.lock:
            if self.video_id == video_id:
                self.video_id = None
                self.pending = self.jpeg = None

    def offer(self, video_id, frame_id, bgr, config):
        with self.lock:
            if not self.closed and self.viewers and self.video_id == video_id:
                # Encoder only reads this array. Retain one latest frame, no copy.
                self.pending = (video_id, frame_id, bgr)
                self.info['config'] = dict(config)
                self.lock.notify_all()

    def subscribe(self):
        with self.lock:
            self.viewers += 1

    def unsubscribe(self):
        with self.lock:
            self.viewers -= 1
            if not self.viewers:
                self.pending = self.jpeg = None

    def snapshot(self):
        with self.lock:
            return dict(ue_alias=self.alias, active=self.video_id is not None,
                        video_id=self.video_id, viewers=self.viewers, max_preview_fps=5,
                        error=self.error, **self.info)

    def work(self):
        next_frame = 0
        while True:
            with self.lock:
                if self.closed:
                    return
                delay = next_frame - time.monotonic()
                if not self.viewers or self.pending is None or delay > 0:
                    self.lock.wait(timeout=max(.01, delay) if delay > 0 else .2)
                    continue
                video, frame, bgr = self.pending
                self.pending = None
            next_frame = time.monotonic() + .2
            try:
                jpeg = self.encode(bgr)
                with self.lock:
                    if self.viewers and self.video_id == video:
                        self.jpeg = jpeg
                        self.sequence += 1
                        self.info['frame_id'] = frame
                        self.error = None
                        self.lock.notify_all()
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)

    def start(self):
        preview = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/':
                    body, mime = HTML, 'text/html; charset=utf-8'
                elif self.path == '/api/status':
                    body, mime = json.dumps(preview.snapshot()).encode(), 'application/json'
                elif self.path == '/preview.mjpg':
                    self.stream()
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def stream(self):
                self.connection.settimeout(2)
                preview.subscribe()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    sequence = -1
                    while not preview.closed:
                        # Detect closed tabs even when the UE has no active source.
                        if select.select([self.connection], [], [], 0)[0]:
                            return
                        with preview.lock:
                            jpeg, current = preview.jpeg, preview.sequence
                            if jpeg is None or current == sequence:
                                preview.lock.wait(timeout=.2)
                                continue
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n' +
                            f'Content-Length: {len(jpeg)}\r\n\r\n'.encode() + jpeg + b'\r\n')
                        self.wfile.flush()
                        sequence = current
                except (OSError, ConnectionError):
                    pass
                finally:
                    preview.unsubscribe()

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.worker = threading.Thread(target=self.work, name='ue-preview-jpeg', daemon=True)
        self.http = threading.Thread(target=self.server.serve_forever, name='ue-preview-http', daemon=True)
        self.worker.start()
        self.http.start()

    def close(self):
        with self.lock:
            self.closed = True
            self.pending = self.jpeg = None
            self.lock.notify_all()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.http.join(timeout=3)
            self.worker.join(timeout=3)
