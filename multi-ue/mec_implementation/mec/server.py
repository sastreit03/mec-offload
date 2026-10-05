"""python -m mec.server --config config.yaml (one control process only)."""
import argparse
import asyncio
import signal

from websockets.asyncio.server import serve
from .config import load
from .control import ControlHub
from .service import MEC


async def run(config_path):
    cfg, catalog = load(config_path)
    mec = MEC(cfg, catalog)
    hub = ControlHub(mec)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            pass
    runner = asyncio.create_task(mec.run())
    stopper = asyncio.create_task(stopping.wait())
    try:
        async with serve(hub.session, cfg.network.bind_host, cfg.network.control_port,
                         max_size=None, max_queue=16, compression=None):
            print(f"Control: ws://{cfg.network.advertise_host}:{cfg.network.control_port}{cfg.network.control_path}")
            print(f"Run logs: {mec.log.path}", flush=True)
            done, _ = await asyncio.wait([runner, stopper], return_when=asyncio.FIRST_COMPLETED)
            if runner in done:
                await runner  # A handled fatal error still executes final snapshot.
            await hub.close()
    finally:
        stopper.cancel()
        runner.cancel()
        await asyncio.gather(stopper, runner, return_exceptions=True)
        await mec.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
