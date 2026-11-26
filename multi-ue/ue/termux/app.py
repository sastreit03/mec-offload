"""Start a localhost UI. Only an explicit POST starts a MEC connection."""
import argparse
import asyncio
import ipaddress
import json
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from handset import Handset

DEFAULT = dict(server="192.168.72.136", port=8765, ue_alias="pixel1",
               folder="/sdcard/Movies/AetheredgeEncoded", bind_ip="", application="detection")


class Controller:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.prefs = root / "settings.json"
        self.settings = DEFAULT | (json.loads(self.prefs.read_text()) if self.prefs.exists() else {})
        self.client = self.task = None
        self.shutdown_requested = False
        self.error = ""

    def status(self):
        c = self.client
        active = bool(self.task and not self.task.done())
        connected = bool(active and c.ws and c.connection and not c.stopped)
        return dict(settings=self.settings, active=active, websocket=("Connected" if connected else
                    "Connecting" if active else "Disconnected"), source=c.args.source_ip if c else "",
                    attempt=c.attempt_id if c else "", health=c.health if c else {},
                    error=self.error, logs=str(c.root) if c else "")

    async def action(self, path, data):
        if path == "/api/status":
            return self.status()
        if path == "/api/events":
            if self.status()["active"]:
                raise ValueError("Disconnect after the final MEC drain before downloading evidence")
            if self.client is None:
                raise ValueError("No client evidence in this service session")
            return self.client.root / "events.jsonl"
        if path == "/api/disconnect":
            await self.stop()
            return self.status()
        if path != "/api/connect":
            raise ValueError("Unknown action")
        if self.shutdown_requested or self.status()["active"]:
            raise ValueError("Already active or shutting down")
        s = {k:data.get(k, v) for k,v in DEFAULT.items()}
        s["server"] = str(ipaddress.IPv4Address(s["server"]))
        s["port"] = int(s["port"])
        if not 1 <= s["port"] <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s["ue_alias"]):
            raise ValueError("Enter a valid port and campaign UE alias")
        if s["bind_ip"]:
            s["bind_ip"] = str(ipaddress.IPv4Address(s["bind_ip"]))
        if s["application"] not in ("detection","tracking","segmentation"):
            raise ValueError("Unknown application")
        folder = Path(s["folder"]).expanduser().resolve()
        if not folder.is_dir() or not any(folder.glob("*.pav")):
            raise ValueError("No staged .pav files found. Check the directory and Termux storage permission.")
        s["folder"] = str(folder)
        logs = self.root / f"{s['server']}-{s['port']}" / s["ue_alias"]
        args = SimpleNamespace(**s, logs=logs, source_dir=None, source="", source_interface=None,
                               source_ip=None, preview_port=0, shuffle_seed=None)
        tmp = self.prefs.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(self.prefs)
        self.settings = s
        self.error = ""
        self.client = Handset(args)
        self.task = asyncio.create_task(self.client.run())
        self.task.add_done_callback(self.finished)
        return self.status()

    def finished(self, task):
        if not task.cancelled() and task.exception():
            self.error = str(task.exception())

    async def stop(self):
        task, c = self.task, self.client
        if task and not task.done() and not c.stopped:
            c.end_samples()
            c.stopped = True
            if c.streamer:
                c.streamer.stop()
            if c.ws:
                await c.ws.close()
            # A pending TCP connection has a five-second timeout.
        if task:
            await asyncio.gather(task, return_exceptions=True)


async def serve(port, root):
    loop = asyncio.get_running_loop()
    controller = Controller(root)
    page = Path(__file__).with_name("index.html").read_bytes()
    host = f"127.0.0.1:{port}"
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data, mime):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def request(self):
            if self.headers.get("Host") != host:
                return self.send_error(403)
            if self.command == "GET" and self.path == "/":
                return self.reply(200, page, "text/html; charset=utf-8")
            data = {}
            try:
                if self.command == "POST" and self.path in ("/api/connect", "/api/disconnect"):
                    if self.headers.get("Origin") != "http://" + host or self.headers.get_content_type() != "application/json":
                        return self.send_error(403)
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= 8192:
                        return self.send_error(400)
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("Expected settings")
                elif self.command != "GET" or self.path not in ("/api/status", "/api/events"):
                    return self.send_error(404)
                result = asyncio.run_coroutine_threadsafe(controller.action(self.path, data), loop).result()
                if self.path == "/api/events":
                    # Export only this client's events, never identity or replay databases.
                    with result.open("rb") as stream:
                        stream.seek(0, 2)
                        remaining = stream.tell()
                        stream.seek(0)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(remaining))
                        self.end_headers()
                        while remaining:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
                self.reply(200, json.dumps(result).encode(), "application/json")
            except (ValueError, TypeError, OSError) as exc:
                self.reply(400, json.dumps(dict(error=str(exc))).encode(), "application/json")

        do_POST = do_GET = request
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Pixel UE controls: http://{host}/  (disconnected at launch)", flush=True)
    done = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, done.set)
    try:
        await done.wait()
    finally:
        controller.shutdown_requested = True
        await controller.stop()
        await asyncio.to_thread(server.shutdown)
        server.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ui-port", type=int, default=8767)
    p.add_argument("--state-dir", type=Path, default=Path.home()/".local/state/mec-pixel")
    args = p.parse_args()
    asyncio.run(serve(args.ui_port, args.state_dir))
