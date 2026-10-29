import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "campaign_manifest", ROOT / "tools/generate_campaign_manifest.py"
)
MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANIFEST)


def inventory(config):
    return {
        "pools": {
            ue: [
                {
                    "media_id": f"video-{number:03d}",
                    "path": f"/datasets/campaign/video-{number:03d}.mov",
                    "sha256": f"{number + 1:064x}",
                    "source_frames": 1212,
                }
                for number in range(42)
            ]
            for ue in config["ues"]
        }
    }


def test_fixed_campaign_expands_to_2664_stable_joinable_scenarios():
    config = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-fixed-policy-v1.yaml").read_text()
    )
    catalog = yaml.safe_load((ROOT / "config/tasks.yaml").read_text())

    first = MANIFEST.build(config, catalog, inventory(config))
    second = MANIFEST.build(config, catalog, inventory(config))

    assert len(first) == 2664
    assert [x["scenario_id"] for x in first] == [x["scenario_id"] for x in second]
    assert len({x["scenario_id"] for x in first}) == 2664
    assert {x["channel"]["speed_mps"] for x in first} == {None, 0, 1, 10}
    assert all(x["channel"]["scope"] == "common_uplink" for x in first)
    assert all(x["policies"]["cuda_priority"] == -3 for x in first)
    assert all(x["ues"] for x in first)


def test_channel_seed_is_shared_across_nonchannel_ablations():
    config = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-fixed-policy-v1.yaml").read_text()
    )
    catalog = yaml.safe_load((ROOT / "config/tasks.yaml").read_text())
    scenarios = MANIFEST.build(config, catalog, inventory(config))
    seeds = {}
    for scenario in scenarios:
        condition = scenario["channel"]["condition_id"]
        seeds.setdefault(condition, set()).add(scenario["channel"]["realization_seed"])
    assert len(seeds) == 111
    assert all(len(values) == 1 for values in seeds.values())


def test_media_segments_are_unique_by_block_and_shared_by_ablation():
    config = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-fixed-policy-v1.yaml").read_text()
    )
    catalog = yaml.safe_load((ROOT / "config/tasks.yaml").read_text())
    scenarios = MANIFEST.build(config, catalog, inventory(config))

    observed = {}
    for scenario in scenarios:
        condition = scenario["factors"]["channel_condition_id"]
        rotation = scenario["factors"]["application_rotation"]
        for ue in scenario["ues"]:
            key = (condition, rotation, ue["ue_alias"])
            clip = (ue["media_id"], ue["source_start_frame"], ue["frame_count"])
            observed.setdefault(key, set()).add(clip)

    # Tiers and the one/four-UE cases reuse the same clip for a block.
    assert all(len(clips) == 1 for clips in observed.values())
    for ue in config["ues"]:
        clips = {
            next(iter(values))
            for key, values in observed.items()
            if key[2] == ue
        }
        assert len(clips) == 333

