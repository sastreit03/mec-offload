from __future__ import annotations
import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from .config import load
from .engine import Engine
from mec_contracts.wire import envelope, loads, dumps


# Probe traffic supports radio association and is excluded from frame accounting.
class ProbeReceiver(asyncio.DatagramProtocol):
    def __init__(self, engine):
        self.engine = engine
        self.counts = {}
        self.last = 0

    def datagram_received(self, data, addr):
        # Fixed probe header. Raw payload never enters inference or frame compliance.
        if not data.startswith(b"MECP1 "):
            return
        try:
            header = data.split(b"\n", 1)[0].decode().split(" ")
            _, uid, connection, probe, index = header
            u = self.engine.ues.get(uid)
            if not u or u.connection_id != connection:
                return
            key = (uid, probe, int(index))
            self.counts[key] = self.counts.get(key, 0) + len(data)
            now = time.monotonic()
            if now - self.last >= 0.1:
                for (uid, p, i), size in self.counts.items():
                    self.engine.aware(
                        "probe_receipt",
                        ue_id=uid,
                        probe_id=p,
                        interval_index=i,
                        bytes=size,
                        measurement_unix_ns=time.time_ns(),
                    )
                self.counts.clear()
                self.last = now
        except (ValueError, UnicodeError):
            return


def create_app(engine):
    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: ProbeReceiver(engine),
            local_addr=(engine.cfg.listen, engine.cfg.control_port + 1),
        )
        try:
            yield
        finally:
            transport.close()
            await engine.close()

    app = FastAPI(title="Streaming MEC", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    @app.get("/readyz")
    def ready():
        r = engine.readiness()
        return JSONResponse(r, status_code=200 if r["ready"] else 503)

    @app.get("/api/status")
    def status():
        return dict(
            readiness=engine.readiness(),
            run_id=engine.cfg.run_id,
            videos={
                v.vid: dict(
                    ue_id=v.uid, application=v.app, state=v.state, counters=v.counters
                )
                for v in engine.videos.values()
            },
            unresolved=len(engine.frames),
            radio_report_age_s=(
                time.time_ns() - engine.last_report["window_end_unix_ns"]
            )
            / 1e9
            if engine.last_report
            else None,
        )

    @app.get("/")
    def dashboard():
        return HTMLResponse("""<!doctype html><html><meta charset="utf-8"><title>Streaming MEC</title>
        <style>body{font:16px system-ui;background:#101820;color:#e9eff4;max-width:1000px;margin:40px auto}pre{white-space:pre-wrap}</style>
        <h1>Streaming MEC</h1><pre id="state">Connecting…</pre><script>
        async function refresh(){try{const r=await fetch('/api/status');document.getElementById('state').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.getElementById('state').textContent='Connection unavailable'}}setInterval(refresh,1000);refresh();</script></html>""")

    @app.websocket("/ws/ue")
    # Each connection has its own outgoing queue and generation fence.
    async def control(ws: WebSocket):
        await ws.accept()
        out = asyncio.PriorityQueue(maxsize=128)
        sequence = 0

        def enqueue(m):
            nonlocal sequence
            sequence += 1
            priority = 1 if m["type"] == "frame_result" else 0
            try:
                out.put_nowait((priority, sequence, m))
            except asyncio.QueueFull:
                engine.record.event("socket_queue_full", message_type=m["type"])
                if m["type"] != "frame_result":
                    asyncio.create_task(ws.close(code=1013))

        u, hello = engine.hello(
            ws.headers.get("x-mec-resume-token"),
            ws.client.host if ws.client else "",
            enqueue,
        )
        generation = u.generation
        await ws.send_text(dumps(hello))

        # One coroutine serializes socket writes; lifecycle control outranks frame results.
        async def sender():
            while True:
                _, _, m = await out.get()
                await ws.send_text(await asyncio.to_thread(dumps, m))
                engine.record.event(
                    "send_attempt",
                    ue_id=u.uid,
                    result_id=m.get("result_id"),
                    message_type=m["type"],
                    delivery="live_or_replay",
                )
                if m["type"] == "connection_replaced":
                    await ws.close(code=1000)
                    return

        async def replay():
            # Fixed reconnect snapshot ordered by video/frame, in bounded disk pages.
            cutoff = await asyncio.to_thread(engine.replay.maximum, u.uid)
            offset = 0
            while True:
                page = await asyncio.to_thread(
                    engine.replay.ordered_page, u.uid, cutoff, offset, 16
                )
                if not page:
                    return
                for m in page:
                    while out.full():
                        await asyncio.sleep(0.005)
                    enqueue(m)
                    engine.record.event(
                        "replay_attempt", ue_id=u.uid, result_id=m["result_id"]
                    )
                offset += len(page)
                await asyncio.sleep(0)

        sending = asyncio.create_task(sender())
        replaying = asyncio.create_task(replay())
        try:
            while True:
                m = loads(await ws.receive_text())
                if engine.ues[u.uid].generation != generation:
                    break
                try:
                    result = await engine.command(u, m)
                    if result:
                        enqueue(result)
                except (ValueError, KeyError, TypeError) as ex:
                    enqueue(envelope("error", engine.cfg.run_id, reason=str(ex)))
        except WebSocketDisconnect:
            pass
        finally:
            engine.disconnect(u.uid, generation)
            sending.cancel()
            replaying.cancel()
            await asyncio.gather(sending, replaying, return_exceptions=True)

    return app


def main():
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    a = p.parse_args()
    cfg, catalog, profiles = load(a.config)
    uvicorn.run(
        create_app(Engine(cfg, catalog, profiles)),
        host=cfg.listen,
        port=cfg.control_port,
        workers=1,
    )


if __name__ == "__main__":
    main()
