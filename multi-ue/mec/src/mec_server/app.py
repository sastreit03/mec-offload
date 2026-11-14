"""
The script is the entrypoint for the MEC server. It does the following:
- Creates the web API for the server using uvicorn and fastapi
- Loads configuration info from config.py
- Starts the MEC engine
- Defines and starts the receiver for the UDP association packets from UEs
- Defines the MEC server's HTTP endpoints
- Defines the websocket endpoint for UE control information including
  the function to place messages to UEs in a priority queue and 
  coroutines to receive new UE connections, send control messages
  to UEs, and replay messages to UEs whose control websocket temporarily
  disconnects.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from .preview import Preview
from .dashboard import HTML as DASHBOARD_HTML
from .config import load
from .engine import Engine
from .control import SendReceipt
from mec_contracts.wire import envelope, loads, dumps


# Probe traffic supports radio association and is excluded from frame accounting.
class ProbeReceiver(asyncio.DatagramProtocol): # DatagramProtocol allows the event loop to notify the app when a UDP packet arrives
    def __init__(self, engine):
        self.engine = engine    # Engine reference used by FastAPI
        self.counts = {}        # Accumulated number of probe bytes
        self.last = 0           # Monotonic time stamp of previous reporting event


    def datagram_received(self, data, addr):    # Data is bytes received in UDP packet, addr is sending IP address
        # Fixed probe header. Raw payload never enters inference or frame compliance.
        if not data.startswith(b"MECP1 "):
            return
        try:
            header = data.split(b"\n", 1)[0].decode().split(" ")    # Parse the header
            _, uid, connection, probe, index = header               # Return fields from the header
            u = self.engine.ues.get(uid)                            # Get UE object for the header's ue_id from engine
            if not u or u.connection_id != connection:
                return
            key = (uid, probe, int(index))                          # Key used for probe association. Index is interval index
            self.counts[key] = self.counts.get(key, 0) + len(data)  # Increment the number of bytes for this UE, this probe, and this interval
                                                                    # Probes are not deduplicated
            now = time.monotonic()
            if now - self.last >= 0.1:   # Send aggregate report. Aggregation window is 0.1 s. Still collect bytes from UDP during aggregation window
                for (uid, p, i), size in self.counts.items():
                    # Submit the association into the engine's awareness queue
                    self.engine.aware(
                        "probe_receipt",
                        ue_id=uid,
                        probe_id=p,
                        interval_index=i,
                        bytes=size,
                        measurement_unix_ns=time.time_ns(),
                    )
                # Clean up
                self.counts.clear()
                self.last = now
        except (ValueError, UnicodeError):
            return


def create_app(engine):
    preview = Preview(engine)
    engine.preview = preview
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
            await preview.close()
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
    async def status():
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
            preview=preview.snapshot(),
            run_id=engine.cfg.run_id,  # Metadata identifier for an experimental run
            scenario=engine.scenario,  # Scenarios for dataset collection
            scenario_error=engine.scenario_error,
            scenario_idle=engine.idle(),
            scenario_start_unix_ns=engine.scenario_start_unix_ns, # Scenarios for dataset collection
            awareness_generation=engine.awareness_generation,     # Used for synchronizing MEC reports to EdgeRIC, increments when MEC restarts
            ues={
                u.uid: dict(
                    active_video=u.active_video,
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
                        and (engine.last_report or {}).get("ues", {}).get(u.uid, {}).get("connection_id") == u.connection_id
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

    @app.post("/api/scenario/finalize")
    async def finalize_scenario(value: dict):
        try:
            return await engine.finalize_scenario(value)
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
            return await engine.start_scenario(value)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    # Dashboard endpoint
    @app.get("/")
    def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/preview.mjpg")
    async def preview_stream(request: Request, ue_alias: str):
        if not any(u.connected and u.alias == ue_alias for u in engine.ues.values()):
            raise HTTPException(404, "No connected UE with this alias")

        async def generate():
            channel = preview.watch(ue_alias)
            sequence = -1
            try:
                while not await request.is_disconnected():
                    if not any(u.connected and u.alias == ue_alias for u in engine.ues.values()):
                        return
                    if (channel.jpeg is not None and channel.sequence != sequence
                            and channel.video_id == preview.video(ue_alias)):
                        sequence = channel.sequence
                        jpeg = channel.jpeg
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\n" +
                               f"Content-Length: {len(jpeg)}\r\n\r\n".encode() + jpeg + b"\r\n")
                    await asyncio.sleep(0.05)
            finally:
                await preview.unwatch(ue_alias, channel)

        return StreamingResponse(generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.websocket("/ws/ue")
    # Each connection has its own outgoing queue and generation fence.
    async def control(ws: WebSocket):
        await ws.accept()
        # A five-second collection run can finish 150 frames in a burst while
        # the terminal status waits behind them.
        out = asyncio.PriorityQueue(maxsize=1024 if engine.collection_mode else 128)  # Queue for outgoing websocket messages
        sequence = 0  # Sequence of messages
        receipts = set()

        # Put messages with their priorities into a priority queue of outgoing messages
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
            receipt = SendReceipt() if m["type"] == "scenario_start" else None
            try:
                # Put messages into queue to send to UE
                # Messages have priority, sequence, and the actual message
                out.put_nowait((priority, sequence, m, receipt))
                if receipt is not None:
                    receipts.add(receipt)
            except asyncio.QueueFull:
                if receipt is not None:
                    receipt.finish("socket_queue_full")
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
            return receipt

        # u: engine's UE object
        # hello: dict with initial message for UE
        u, hello = engine.hello(
            ws.headers.get("x-mec-resume-token"),   # Resume token for an existing UE that reconnects, part of the HTTP header
            ws.client.host if ws.client else "",    # UE IP address from websocket. Engine stores it as u.peer_ip
            enqueue,                                # Callback to enqueue message. Assigned for each UE object so engine can send messages to UE through the UE's websocket
        )
        generation = u.generation           # The generation of the UE's connection. Increments when a UE reconnects
        await ws.send_text(dumps(hello))    # Send the hello message through the websocket

        # One coroutine serializes socket writes; lifecycle control outranks frame results.
        async def sender():
            while True:
                # Continuously read messages from the priority queue and send them over the websocket
                _, _, m, receipt = await out.get()
                # Because of await, execution of this coroutine halts here until a message is in the UE's priority queue
                # Consecutive message sends are performed synchronously, one after the other
                # to_thread moves the serialization of the JSON from dumps into a worker thread
                try:
                    if receipt is not None:
                        if receipt.done.cancelled():
                            continue
                        if (engine.scenario_error or engine.scenario_finalization
                                or u.generation != generation or not u.connected
                                or engine.scenario is None
                                or any(m.get(k) != v for k, v in engine.scenario.items())):
                            receipt.finish("obsolete_start")
                            continue
                        # Tiny control message: avoid a thread hop before timing its write.
                        payload = dumps(m)
                        receipt.started_ns = time.monotonic_ns()
                        await ws.send_text(payload)
                        receipt.finish()
                    else:
                        await ws.send_text(await asyncio.to_thread(dumps, m))
                except Exception as error:
                    if receipt is not None:
                        receipt.finish(str(error))
                    engine.disconnect(u.uid, generation)
                    await ws.close(code=1011)
                    return
                finally:
                    if receipt is not None:
                        receipts.discard(receipt)
                # Record that a message was sent
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
                # If a websocket connection is replaced due to a UE reconnecting, close the old websocket
                if m["type"] == "connection_replaced":
                    await ws.close(code=1000)
                    return

        # Handle the return of frame results to the UE while a UE's control websocket was disconnected
        async def replay():
            # Fixed reconnect snapshot ordered by video/frame, in bounded disk pages.
            cutoff = await asyncio.to_thread(engine.replay.maximum, u.uid)  # Cutoff is when the results are no longer to be stored
            offset = 0
            while True:
                # A page is a batch of 16 results that were saved
                page = await asyncio.to_thread(
                    engine.replay.ordered_page, u.uid, cutoff, offset, 16
                )
                if not page:
                    return
                for m in page:
                    # handle a full queue
                    while out.full():
                        await asyncio.sleep(0.005)
                    enqueue(m)
                    # Record the replay attempt
                    engine.record.event(
                        "replay_attempt",
                        ue_id=u.uid,
                        result_id=m["result_id"],
                        **{
                            key: m.get(key)
                            for key in ("campaign_id", "scenario_id", "attempt_id")
                        },
                    )
                # Offset to grab the next stored results
                offset += len(page)
                await asyncio.sleep(0)

        # Create tasks for sending and replaying in the current control loop
        sending = asyncio.create_task(sender())
        replaying = asyncio.create_task(replay())
        try:
            while True:
                # Asynchronously wait for message from UE
                m = loads(await ws.receive_text())
                # Check if UE's generation is the latest
                if engine.ues[u.uid].generation != generation:
                    break
                try:
                    # Pass message from UE to engine and wait for engine's response
                    result = await engine.command(u, m)
                    # queue result from engine
                    if result:
                        enqueue(engine.contextual(result))
                # Error handling
                except (ValueError, KeyError, TypeError) as ex:
                    enqueue(
                        engine.contextual(
                            envelope("error", engine.cfg.run_id, reason=str(ex))
                        )
                    )
        except WebSocketDisconnect:
            pass
        finally:
            for receipt in receipts:
                receipt.finish("connection_closed")
            engine.disconnect(u.uid, generation)    # Disconnect UE of current generation in engine
            sending.cancel()                        # Cancel tasks in event loop and wait for them to cancel in .gather
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
