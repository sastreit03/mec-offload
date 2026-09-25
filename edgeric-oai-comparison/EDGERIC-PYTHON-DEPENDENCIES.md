# EdgeRIC-v2 Python dependency inventory

Inspected 2026-09-24. Scope: all project `.py` files under
`/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2`, excluding the bundled `edgeric/`
virtual environment and bytecode caches. This is a static source inventory,
not a tested installation lockfile. Bundled third-party package source is
not application code and is excluded from the per-file tables.

## Deployment groups (including local-import dependencies)

| Deployment | Required application packages | Runtime services/assets |
| --- | --- | --- |
| Base messenger only | pyzmq, protobuf | Matching generated `*_pb2.py` files; reachable gNB ZeroMQ endpoints |
| `send_weight.py`, `send_weightv2.py`, `send_mcs.py` | pyzmq, protobuf, numpy | gNB metrics/control connection; no Redis or Torch needed |
| muApp1, unmodified (including non-RL scheduling) | pyzmq, protobuf, numpy, gym, pandas, torch, redis | Redis server on localhost:6379 and `scheduling_algorithm` key; RL additionally needs model files and their Python classes |
| muApp3 terminal monitor, unmodified | pyzmq, protobuf, numpy, gym, pandas, torch, redis | gNB connection; imports Redis but does not connect to a Redis server in this script |
| muApp3 Matplotlib monitor | pyzmq, protobuf, numpy, matplotlib, redis | Redis server and graphical backend/display for interactive plots; also has a messenger API mismatch described below |
| muApp3 Prometheus exporter | pyzmq, protobuf, numpy, prometheus-client | HTTP metrics endpoint; Prometheus and Grafana are separate services if desired; no Redis required |
| muApp2 PPO training, including eager package imports | pyzmq, protobuf, numpy, torch, gym, hydra-core, ray[rllib], pandas, Pillow, plotly | Training YAML, configured traces/assets, gNB for live loop, writable outputs; image exports additionally need Kaleido/browser |
| Additional TRPO algorithm | numpy, scipy, torch | Shared local utils package |
| Standalone Plotly analysis | numpy, pandas, plotly | Input CSVs; Kaleido/browser for static images |
| Whole project, all paths | pyzmq, protobuf, numpy, gym, pandas, torch, redis, matplotlib, prometheus-client, hydra-core, ray[rllib], scipy, Pillow, plotly, kaleido | Union of services/assets above; requires compatibility work, not simply latest versions |

These are application-level distributions. Their own transitive packages
(e.g. Hydra's OmegaConf, RLlib's dependencies, Torch's dependencies) are
resolved by pip after versions/platform are selected. No complete resolved
transitive version list can be inferred without a tested environment/lockfile.

## Recorded versions, not a complete requirements lock

The bundled `edgeric/pyvenv.cfg` records Python **3.12.3**, with system packages
excluded. Its distribution metadata records:

| Distribution | Version |
| --- | --- |
| numpy | 2.1.2 |
| pyzmq | 26.2.0 |
| protobuf | 3.20.1 |
| prometheus-client | 0.21.0 |
| pip (installation tool) | 24.0 |
| zmq (placeholder depending on pyzmq; unnecessary to install separately) | 0.0.0 |

Versions for Torch, Gym, pandas, Redis, Ray, Hydra, SciPy, Pillow,
Matplotlib, Plotly, and Kaleido are not recorded in that environment. No
project requirements/lockfile was found. The bundled versions do not prove
that the entire project runs with Python 3.12 or NumPy 2.1.2.

## Import behavior that expands requirements

- `stream_rl/__init__.py` eagerly imports callbacks, datasets, environments,
  policy networks, registry, rewards, and plotting. Even an import of
  `stream_rl.registry` therefore requires the larger training stack,
  including Pillow, Plotly, and Hydra. This also applies when importing a
  plotting submodule through the package instead of running its file directly.
- `utils/__init__.py` eagerly imports the shared helpers. Importing
  `utils.replay_memory` or `utils.tools` through the package pulls in NumPy
  and Torch even though those individual files only import standard-library
  modules. Both `models/mlp_policy*.py` transitively require NumPy via utils.
- muApp1 and the terminal monitor import Gym, pandas, Torch, and Redis
  unconditionally; unused imports still require the packages as written.
- Local `models`, `core`, `utils`, and `stream_rl` are repository code,
  not similarly named packages to install from PyPI. Keep the repository
  root importable (for example `PYTHONPATH=/opt/edgeric`).

## Compatibility and non-pip requirements

- Python 3.11 retains `imp`; Python 3.12 removed it. The training import
  chain reaches `stream_rl/callbacks.py`, which imports it. A venv does not
  repair this API removal. [Python documentation](https://docs.python.org/3.12/library/imp.html).
- Legacy Gym does not support NumPy 2.0. The recorded NumPy 2.1.2 environment
  therefore must not be treated as a validated Gym/training environment.
  Select a tested legacy combination or migrate the code; installing
  Gymnasium alone does not satisfy `import gym`.
  [Gym project notice](https://gymlibrary.dev/).
- Old generated protobuf descriptors require compatible runtimes; the
  recorded 3.20.1 is evidence, not a recommendation to combine it with every
  current training package. Regenerating Python protobuf code is a separate
  modernization option. [Protobuf compatibility](https://protobuf.dev/news/2022-05-06/).
- Whole-model `torch.load` in muApp1/plots needs compatibility review with
  PyTorch 2.6+'s `weights_only` default. Model class imports and checkpoint
  format also matter. [PyTorch notes](https://pytorch.org/blog/pytorch2-6/).
- RLlib imports use legacy APIs (including `Episode` and callback paths).
  Availability of a modern ARM64 Ray wheel does not establish compatibility
  with this source; the exact version set needs testing.
  [Ray installation](https://docs.ray.io/en/latest/ray-overview/installation.html).
- `stream_rl/plots.py` and `stream_rl/plotlyplots.py` call `write_image`:
  install a compatible Kaleido. Kaleido v1 needs a separately installed
  Chrome/Chromium browser and its OS libraries; select an ARM64-compatible
  browser for DGX Spark. Interactive `show()` needs a usable renderer.
  [Plotly documentation](https://plotly.com/python/static-image-export/).
- Python/venv and CA certificates are the basic Ubuntu image requirements.
  With compatible binary wheels, do not assume compiler, libzmq development,
  or protobuf C++ development packages are required. Source builds need
  additional build dependencies determined by the selected package/version.
- CPU messaging/inference does not require UHD, OAI libraries, CUDA, TensorRT,
  or NVIDIA Container Toolkit. GPU training is optional in the training
  script and needs an appropriate CUDA-enabled Torch/container setup.
- Redis is a server, distinct from the `redis` pip client. muApp1 and the
  graphical monitor connect to localhost:6379. Sharing the gNB network
  namespace does not expose host-local Redis at that localhost.
- Copy YAML configurations, selected model files/classes, datasets/traces,
  and writable output mounts for the paths actually used. muApp1 uses
  working-directory-relative `./rl_model/...`; training references paths
  in its Hydra configuration. Inspect those paths before choosing WORKDIR.
- The terminal monitor constructs `EdgericMessenger(socket_type="weights")`
  and binds port 5556 even though it only displays metrics. It will conflict
  with another weight publisher sharing that network namespace.
- The graphical monitor calls `get_metrics_multi()`, absent from the current
  messenger, and expects older metric key names. It also calls
  `redis_db.flushdb()` at startup. Installing packages alone does not make
  that monitor compatible with the current messenger; do not run it against
  a Redis database whose data must be retained.

## Per-file inventory

Every table lists **direct imports**. Apply the deployment-group and eager
import rules above for the full dependency set of a running entry point.
Standard-library modules need no pip installation. Package names below are
pip distribution names (`PIL` → `Pillow`, `hydra` → `hydra-core`, etc.).
The runtime-only Kaleido requirement is recorded above because no file
imports it directly.

### Base messaging and example controllers

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [control_mcs_pb2.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/control_mcs_pb2.py) | `protobuf` | — | `sys` |
| [control_weights_pb2.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/control_weights_pb2.py) | `protobuf` | — | `sys` |
| [edgeric_messenger.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/edgeric_messenger.py) | `pyzmq` | `control_mcs_pb2`, `control_weights_pb2`, `metrics_pb2` | `random`, `time` |
| [edgeric_messenger222.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/edgeric_messenger222.py) | `pyzmq` | `control_mcs_pb2`, `control_weights_pb2`, `metrics_pb2` | `time` |
| [metrics_pb2.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/metrics_pb2.py) | `protobuf` | — | `sys` |
| [send_mcs.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/send_mcs.py) | `numpy` | `edgeric_messenger` | `threading`, `time` |
| [send_weight.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/send_weight.py) | `numpy` | `edgeric_messenger` | `threading`, `time` |
| [send_weightv2.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/send_weightv2.py) | `numpy` | `edgeric_messenger` | `sys`, `threading`, `time` |

### Training entry point and algorithms

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [core/a2c.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/core/a2c.py) | `torch` | — | — |
| [core/agent.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/core/agent.py) | — | `utils.replay_memory`, `utils.torch` | `math`, `multiprocessing`, `os`, `time` |
| [core/agent_original.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/core/agent_original.py) | `numpy`, `pyzmq`, `torch` | `edgeric_messenger`, `utils.replay_memory`, `utils.torch` | `math`, `multiprocessing`, `os`, `time` |
| [core/common.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/core/common.py) | `torch` | `utils` | — |
| [core/ppo.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/core/ppo.py) | `torch` | — | — |
| [core/trpo.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/core/trpo.py) | `numpy`, `scipy` | `utils` | — |
| [muApp2/muApp2_train_RL_DL_scheduling.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/muApp2/muApp2_train_RL_DL_scheduling.py) | `gym`, `hydra-core`, `numpy`, `pyzmq`, `torch` | `core.agent_original`, `core.common`, `core.ppo`, `models.mlp_critic`, `models.mlp_policy`, `models.mlp_policy_disc`, `stream_rl.plots`, `stream_rl.registry`, `utils` | `argparse`, `logging`, `math`, `os`, `pickle`, `sys`, `time` |

### Plotting, analysis, and data preparation

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [debug/operatingpoint.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/debug/operatingpoint.py) | `numpy`, `pandas`, `plotly` | — | — |
| [stream_rl/envs/cqi_traces/trace_generator.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/envs/cqi_traces/trace_generator.py) | `numpy`, `pandas` | — | `random` |
| [stream_rl/extract_reward.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/extract_reward.py) | — | — | `csv`, `re` |
| [stream_rl/plotlyplots.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/plotlyplots.py) | `numpy`, `pandas`, `plotly` | — | — |
| [stream_rl/plots.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/plots.py) | `hydra-core`, `numpy`, `pandas`, `plotly`, `torch` | — | `os` |

### Policy and value models

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [models/mlp_critic.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/models/mlp_critic.py) | `torch` | — | — |
| [models/mlp_discriminator.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/models/mlp_discriminator.py) | `torch` | — | — |
| [models/mlp_policy.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/models/mlp_policy.py) | `torch` | `utils.math` | — |
| [models/mlp_policy_disc.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/models/mlp_policy_disc.py) | `torch` | `utils.math` | — |

### muApp1: scheduling and inference

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [muApp1/muApp1_run_DL_scheduling.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/muApp1/muApp1_run_DL_scheduling.py) | `gym`, `numpy`, `pandas`, `redis`, `torch` | `edgeric_messenger` | `argparse`, `collections`, `datetime`, `math`, `os`, `pickle`, `sys`, `threading`, `time` |

### muApp3: monitoring

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [muApp3/muApp3_monitor.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/muApp3/muApp3_monitor.py) | `matplotlib`, `numpy`, `redis` | `edgeric_messenger` | `collections`, `os`, `sys`, `threading`, `time` |
| [muApp3/muApp3_monitor_grafana.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/muApp3/muApp3_monitor_grafana.py) | `numpy`, `prometheus-client` | `edgeric_messenger` | `sys`, `threading`, `time` |
| [muApp3/muApp3_monitor_terminal.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/muApp3/muApp3_monitor_terminal.py) | `gym`, `numpy`, `pandas`, `redis`, `torch` | `edgeric_messenger` | `argparse`, `collections`, `datetime`, `math`, `os`, `pickle`, `sys`, `threading`, `time` |

### Training framework, environments, and datasets

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [stream_rl/__init__.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/__init__.py) | — | `.callbacks`, `.datasets`, `.envs`, `.plots`, `.policy_net`, `.registry`, `.rewards` | — |
| [stream_rl/callbacks.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/callbacks.py) | `ray[rllib]` | — | `imp`, `typing` |
| [stream_rl/datasets/__init__.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/datasets/__init__.py) | — | `.vimeo90k_video` | — |
| [stream_rl/datasets/vimeo90k_video.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/datasets/vimeo90k_video.py) | `Pillow`, `torch` | — | `pathlib` |
| [stream_rl/envs/__init__.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/envs/__init__.py) | — | `.edge_ric`, `.single_agent_env`, `.streaming_env` | — |
| [stream_rl/envs/edge_ric.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/envs/edge_ric.py) | `gym`, `numpy`, `pandas`, `pyzmq`, `ray[rllib]`, `torch` | `stream_rl.registry` | `collections`, `random`, `time` |
| [stream_rl/envs/simpler_streaming_env.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/envs/simpler_streaming_env.py) | `gym`, `numpy`, `ray[rllib]` | `stream_rl.registry` | `itertools` |
| [stream_rl/envs/single_agent_env.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/envs/single_agent_env.py) | `gym`, `numpy`, `ray[rllib]` | `stream_rl.registry` | `itertools` |
| [stream_rl/envs/streaming_env.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/envs/streaming_env.py) | `gym`, `numpy`, `ray[rllib]` | `stream_rl.registry` | `collections` |
| [stream_rl/policy_net/__init__.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/policy_net/__init__.py) | — | `.conv_policy` | — |
| [stream_rl/policy_net/conv_policy.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/policy_net/conv_policy.py) | `numpy`, `ray[rllib]`, `torch` | `stream_rl.registry` | — |
| [stream_rl/registry/__init__.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/registry/__init__.py) | `ray[rllib]` | — | — |
| [stream_rl/rewards.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/stream_rl/rewards.py) | — | `stream_rl.registry` | — |

### Shared utilities

| Python file | Direct third-party packages | Direct local imports | Standard library |
| --- | --- | --- | --- |
| [utils/__init__.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/utils/__init__.py) | — | `utils.math`, `utils.replay_memory`, `utils.tools`, `utils.torch`, `utils.zfilter` | — |
| [utils/math.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/utils/math.py) | `torch` | — | `math` |
| [utils/replay_memory.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/utils/replay_memory.py) | — | — | `collections`, `random` |
| [utils/tools.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/utils/tools.py) | — | — | `os` |
| [utils/torch.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/utils/torch.py) | `numpy`, `torch` | — | — |
| [utils/zfilter.py](/home/sstreit/EdgeRIC-5G-OAI/EdgeRIC-v2/utils/zfilter.py) | `numpy` | — | — |

Inventory coverage: **47 project Python files**, all parsed successfully. No project code or dependencies were changed.
