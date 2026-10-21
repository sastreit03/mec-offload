import argparse
import asyncio
import yaml
from .runtime import Runtime
from .oai_adapter import OAIAdapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    if cfg["mode"] not in ("shadow", "heuristic"):
        raise ValueError("unsupported policy mode; RL deferred")
    asyncio.run(Runtime(cfg, OAIAdapter()).run())


if __name__ == "__main__":
    main()
