from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from control import ControlClient
from dashboard import router
from models import SharedState, load_config
from network import validate_route, get_interface_ipv4
from streamer import VideoStreamer


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("ue.app")


def create_app(config_path: str) -> FastAPI:
    config = load_config(config_path)
    ue_ip = get_interface_ipv4(config.network.expected_ue_interface)
    shared = SharedState()
    streamer = VideoStreamer(config.video, config.network, shared, ue_ip)
    control = ControlClient(config.network, shared, config.video, ue_ip)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        route = validate_route(
            config.network.mec_ip,
            config.network.expected_ue_interface,
            config.network.strict_route_check,
            ue_ip
        )
        shared.set_route(route)
        LOG.info("Route to MEC: %s", route)

        app.state.shared = shared
        app.state.streamer = streamer
        app.state.control = control

        control_task = asyncio.create_task(control.run(), name="mec-control-client")
        try:
            await asyncio.to_thread(streamer.start)
            yield
        finally:
            await asyncio.to_thread(streamer.stop)
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)

    app = FastAPI(title="OAI UE MEC Video Demo", lifespan=lifespan)
    app.include_router(router)
    # These are assigned here as well so route handlers have predictable attributes
    # in unit tests before lifespan starts.
    app.state.shared = shared
    app.state.streamer = streamer
    app.state.control = control
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="UE H.264/RTP MEC demo")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    cfg = load_config(args.config)
    app = create_app(args.config)
    uvicorn.run(
        app,
        host=cfg.dashboard.host,
        port=cfg.dashboard.port,
        log_level=args.log_level,
        access_log=False,
    )


if __name__ == "__main__":
    main()
