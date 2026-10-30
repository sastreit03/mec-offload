import importlib.util
from itertools import product
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
    assert all(x["channel"]["scope"] == "common_bidirectional" for x in first)
    assert {
        x["channel"]["delay_spread_ns"] for x in first
    } == {None, 0, 100, 300}
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


def test_51prb_radio_profile_changes_scenario_identity():
    legacy = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-fixed-policy-v1.yaml").read_text()
    )
    narrow = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-fixed-policy-51prb-v1.yaml").read_text()
    )
    catalog = yaml.safe_load((ROOT / "config/tasks.yaml").read_text())
    legacy_scenarios = MANIFEST.build(legacy, catalog, inventory(legacy))
    narrow_scenarios = MANIFEST.build(narrow, catalog, inventory(narrow))

    assert len(narrow_scenarios) == 2664
    assert {item["radio"]["profile_id"] for item in narrow_scenarios} == {
        "band78-51prb-threequarter"
    }
    assert all(item["factors"]["radio_profile"] for item in narrow_scenarios)
    assert not (
        {item["scenario_id"] for item in legacy_scenarios}
        & {item["scenario_id"] for item in narrow_scenarios}
    )


def test_pass_through_pilot_has_one_channel_condition():
    config = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-pass-through-51prb-pilot.yaml").read_text()
    )
    catalog = yaml.safe_load((ROOT / "config/tasks.yaml").read_text())
    scenarios = MANIFEST.build(config, catalog, inventory(config))

    assert len(scenarios) == 24
    assert {item["channel"]["condition_id"] for item in scenarios} == {
        "pass-through"
    }
    assert {item["channel"]["model"] for item in scenarios} == {
        "PASS_THROUGH"
    }


def test_no_emulator_campaign_has_780_independent_paired_tier_scenarios():
    config = yaml.safe_load(
        (ROOT / "config/campaigns/aetheredge-paired-per-ue-51prb-no-emulator-v1.yaml").read_text()
    )
    catalog = yaml.safe_load((ROOT / "config/tasks.yaml").read_text())
    scenarios = MANIFEST.build(config, catalog, inventory(config))

    one_ue = [x for x in scenarios if x["factors"]["ue_set"] == "one_ue"]
    four_ue = [x for x in scenarios if x["factors"]["ue_set"] == "four_ue"]
    assert len(one_ue) == 12
    assert len(four_ue) == 768
    assert all(x["channel"]["emulation_enabled"] is False for x in scenarios)
    assert {x["channel"]["condition_id"] for x in scenarios} == {"physical-rf"}
    assert all("realization_seed" not in x["channel"] for x in scenarios)
    assert all(len(x["factors"]["tier_assignment"]) == len(x["ues"]) for x in scenarios)
    assert {
        tuple(x["factors"]["tier_assignment"][ue] for ue in config["ue_sets"]["four_ue"])
        for x in four_ue
    } == set(product(range(4), repeat=4))
    for scenario in scenarios:
        assigned = scenario["factors"]["tier_assignment"]
        assert all(item["tier"] == assigned[item["ue_alias"]] for item in scenario["ues"])
    for ue in config["ues"]:
        clips = [
            (item["media_id"], item["source_start_frame"])
            for scenario in scenarios
            for item in scenario["ues"]
            if item["ue_alias"] == ue
        ]
        assert len(set(clips)) == 336
        uses = {clip: clips.count(clip) for clip in set(clips)}
        assert min(uses.values()) == 2
        assert max(uses.values()) == 3

