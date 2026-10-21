import asyncio
from pathlib import Path
import yaml
from muApp_mec.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def test_shadow_runtime_never_publishes_weight_actions(tmp_path):
    class Adapter:
        def __init__(self):
            self.sent = []
            self.index = 0
            self.closed = False

        def receive(self):
            self.index += 1
            return self.index, {100: dict(rx_bytes=10)}

        def send(self, index, weights):
            self.sent.append((index, weights))

        def close(self):
            self.closed = True

    async def exercise():
        cfg = yaml.safe_load((ROOT / "config/edgeric.shadow.yaml").read_text())
        cfg.update(
            listen="127.0.0.1:0",
            trace_file=str(tmp_path / "none"),
            runs_dir=str(tmp_path / "edge"),
        )
        adapter = Adapter()
        runtime = Runtime(cfg, adapter)
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.12)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert adapter.index > 1 and adapter.sent == [] and adapter.closed
        assert (runtime.root / "associations.json").is_file()

    asyncio.run(exercise())
