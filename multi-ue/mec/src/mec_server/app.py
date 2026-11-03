from __future__ import annotations
import argparse
import asyncio
import json
import time
from collections import Counter
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
        # Initialize engine and UDP receiver for association probe
        await engine.start()
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: ProbeReceiver(engine),
            local_addr=(engine.cfg.listen, engine.cfg.control_port + 1),
        )
        # Return execution back to FastAPI
        try:
            yield
        # Close the app
        finally:
            transport.close()
            await engine.close()

    # FastAPI controls application logic of web API
    app = FastAPI(title="Streaming MEC", lifespan=lifespan)
    app.state.engine = engine

    # GET whether the server is alive; heartbeat
    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    # GET whether the server is ready to run
    @app.get("/readyz")
    def ready():
        r = engine.readiness()
        return JSONResponse(r, status_code=200 if r["ready"] else 503)

    @app.get("/api/status")
    def status():
        # Aetheredge: expose ingestion separately from terminal outcomes. An
        # ordered tracking stream can receive many frames while an incomplete
        # predecessor temporarily holds all tracker completions.
        pipeline = {}
        for frame in engine.frames.values():
            # Number of frames currently tracked by MEC, by video and by status.
            # Current count of frames in memory
            pipeline.setdefault(frame.vid, Counter())[frame.state] += 1
        return dict(
            readiness=engine.readiness(), 
            run_id=engine.cfg.run_id,  # Metadata identifier for an experimental run
            scenario=engine.scenario,  # Scenarios for dataset collection
            scenario_start_unix_ns=engine.scenario_start_unix_ns, # Scenarios for dataset collection
            awareness_generation=engine.awareness_generation,     # Used for synchronizing MEC reports to EdgeRIC, increments when MEC restarts
            ues={
                u.uid: dict(
                    alias=u.alias,          # Used in dataset collection. e.g., pixel1, linux1
                    connected=u.connected,  # whether the UE is connected to the MEC server via the control connection
                    ip=u.ip,                # UE's IP address, used for EdgeRIC association
                    scenario_acknowledged=bool(
                        engine.scenario     # Scenario for dataset collection
                        and u.scenario_ack  # Used in data set collection. Whether UE has acknowledged the scenario that is armed
                        == (
                            engine.scenario["attempt_id"], # Scenario for dataset collection
                            u.generation,  # Generation of UE connection, increments when UE's control channel reconnects
                            u.alias,       # Used in dataset collection. e.g., pixel1, linux1
                        )
                    ),
                    association_valid=bool(
                        (engine.last_report or {})
                        .get("ues", {})
                        .get(u.uid, {})
                        .get("association_valid")
                    ),  # Whether the EdgeRIC has an association between the UE's IP address and its RNTI
                )
                for u in engine.ues.values()
            },
            videos={
                v.vid: dict(
                    ue_id=v.uid,                            # Unique identifier for the video
                    ue_alias=engine.ues[v.uid].alias,       # Used in dataset collection. e.g., pixel1, linux1
                    application=v.app,                      # Inference task, e.g., detection or segmentation
                    candidate_id=v.requested_candidate_id,  # Requested bitrate/DNN configuration
                    state=v.state,                          # State of entire video (not just of a frame), e.g., COMPLETED, DENIED
                    counters=v.counters,                    # The number of frames of the video by terminal outcome, e.g., completed, dropped
                    received_frames=v.received,             # Number of valid frames received for a video
                    pipeline_states=dict(pipeline.get(v.vid, {})),  # Counts the number of unresolved frames, e.g., queued, processing
                    final_frame_id=v.final_id,              # Final frame ID of the video
                    collection_error=v.collection_error,    # Used in dataset collection. Whether an error has occurred in a frame of the video
                )
                for v in engine.videos.values()
            },
            unresolved=len(engine.frames),  # How many frames don't have terminal outcomes
            radio_report_age_s=(
                time.time_ns() - engine.last_report["window_end_unix_ns"]
            )
            / 1e9 # Age of last report
            if engine.last_report
            else None,
        )

    # For dataset generation, load/arm a given scenario
    @app.post("/api/scenario/arm")
    async def arm_scenario(value: dict):
        try:
            return engine.arm_scenario(value)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    # For dataset generation, start a given scenario
    @app.post("/api/scenario/disarm")
    async def disarm_scenario(value: dict):
        try:
            return engine.disarm_scenario(value)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    # For dataset generation, start a given scenario
    @app.post("/api/scenario/start")
    async def start_scenario(value: dict):
        try:
            return engine.start_scenario(value)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    # Dashboard endpoint
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
        # A five-second collection run can finish 150 frames in a burst while
        # the terminal status waits behind them.
        out = asyncio.PriorityQueue(maxsize=1024 if engine.collection_mode else 128)  # Queue for outgoing websocket messages
        sequence = 0  # Sequence of messages

        def enqueue(m):
            nonlocal sequence
            sequence += 1
            # Terminal collection status follows every already-enqueued frame result.
            terminal = m["type"] == "video_status" and m.get("status") in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "EXPIRED",
            )
            # Lower priority number is actually higher priority
            priority = 2 if terminal else 1 if m["type"] == "frame_result" else 0
            try:
                # Put messages into queue to send to UE
                # Messages have priority, sequence, and the actual message
                out.put_nowait((priority, sequence, m))
            except asyncio.QueueFull:
                # Exception for the queue being full
                engine.record.event(
                    "socket_queue_full",
                    message_type=m["type"],
                    **{
                        key: m.get(key)
                        for key in ("campaign_id", "scenario_id", "attempt_id")
                    },
                )
                if engine.collection_mode or m["type"] != "frame_result":
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
                    **{
                        key: m.get(key)
                        for key in ("campaign_id", "scenario_id", "attempt_id")
                    },
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
                        "replay_attempt",
                        ue_id=u.uid,
                        result_id=m["result_id"],
                        **{
                            key: m.get(key)
                            for key in ("campaign_id", "scenario_id", "attempt_id")
                        },
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
                        enqueue(engine.contextual(result))
                except (ValueError, KeyError, TypeError) as ex:
                    enqueue(
                        engine.contextual(
                            envelope("error", engine.cfg.run_id, reason=str(ex))
                        )
                    )
        except WebSocketDisconnect:
            pass
        finally:
            engine.disconnect(u.uid, generation)
            sending.cancel()
            replaying.cancel()
            await asyncio.gather(sending, replaying, return_exceptions=True)

    return app


def main():
    # Uvicorn handles HTTP messaging, faces the network
    import uvicorn

    # Parse the command line arguments
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    a = p.parse_args()

    # Load configuration, task model catalog, and profiled dataa
    cfg, catalog, profiles = load(a.config)

    # Run uvicorn web API
    uvicorn.run(
        create_app(Engine(cfg, catalog, profiles)),
        host=cfg.listen,        # IP address for web API to listen on
        port=cfg.control_port,  # Port for web API to listen on 
        workers=1,              # One worker for same control context
        # The legacy websockets protocol can concurrently drain an application
        # result and an automatic pong under radio backpressure, tripping its
        # single-waiter assertion.  Uvicorn's sans-I/O backend serializes the
        # transport without that legacy FlowControlMixin path.
        ws="websockets-sansio",
    )


if __name__ == "__main__":
    main()
