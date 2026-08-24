from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket

from control import ControlHub
from dashboard import router as dashboard_router
from models import FrameBuffer, SharedState, load_config
from receiver import VideoReceiver
from scheduler import InferenceScheduler
from vlm import VLMEngine


LOG = logging.getLogger("mec.app")


def create_app(config_path: str) -> FastAPI:
    cfg = load_config(config_path)
    state = SharedState()
    frame_buffer = FrameBuffer(cfg.receiver.frame_buffer_max_frames)
    receiver = VideoReceiver(cfg.network, cfg.receiver, state, frame_buffer)
    engine = VLMEngine(cfg.vlm, state)
    scheduler = InferenceScheduler(cfg.vlm, state, frame_buffer, engine)
    control = ControlHub(
        state,
        frame_buffer,
        cfg.vlm,
        on_question_changed=scheduler.wake,
    )
    scheduler.attach_control(control)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg
        app.state.shared = state
        app.state.frame_buffer = frame_buffer
        app.state.receiver = receiver
        app.state.engine = engine
        app.state.scheduler = scheduler
        app.state.control = control

        # Start receiving video immediately. Load the VLM concurrently so the
        # dashboard/media path can be debugged even while model initialization occurs.
        await asyncio.to_thread(receiver.start)
        model_task = asyncio.create_task(asyncio.to_thread(engine.load))
        scheduler_task = asyncio.create_task(scheduler.run())
        try:
            yield
        finally:
            scheduler_task.cancel()
            model_task.cancel()
            await asyncio.gather(scheduler_task, model_task, return_exceptions=True)
            await asyncio.to_thread(receiver.stop)

    app = FastAPI(title="MEC Video VLM Demo", lifespan=lifespan)
    app.include_router(dashboard_router)

    @app.websocket(cfg.network.control_path)
    async def ue_control_socket(ws: WebSocket) -> None:
        await control.websocket_session(ws)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="MEC RTP/H.264 + Qwen3-VL demo")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    app = create_app(args.config)
    # One Uvicorn process only. The GPU model and GStreamer receiver are process-local.
    uvicorn.run(
        app,
        host=cfg.network.control_host,
        port=cfg.network.control_port,
        log_level=args.log_level,
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
