#!/usr/bin/env python3
"""Expand an Aetheredge campaign definition into an immutable scenario list."""

import argparse
import hashlib
import json
import random
import string
from itertools import product
from pathlib import Path

import yaml


APPLICATIONS = ("detection", "tracking", "segmentation")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_yaml(path):
    value = yaml.safe_load(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def candidate_index(catalog):
    return {
        (item["application"], item["model"], int(item["bitrate_bps"])): item["id"]
        for item in catalog["configurations"]
    }


def channel_conditions(config):
    channels = config["channels"]
    scope = channels["scope"]
    if scope != "common_bidirectional":
        raise ValueError(
            "this campaign requires channels.scope=common_bidirectional"
        )
    conditions = []
    if channels.get("include_awgn"):
        for sinr in channels["sinr_db"]:
            conditions.append(
                dict(
                    condition_id=f"awgn-sinr{sinr}",
                    model="AWGN",
                    sinr_db=sinr,
                    delay_spread_ns=None,
                    speed_mps=None,
                    scope=scope,
                )
            )
    for model, sinr, delay, speed in product(
        channels["statistical_models"],
        channels["sinr_db"],
        channels["delay_spreads_ns"],
        channels["speeds_mps"],
    ):
        slug = model.lower()
        conditions.append(
            dict(
                condition_id=(
                    f"{slug}-sinr{sinr}-delay-spread{delay}-speed{speed}"
                ),
                model=model,
                sinr_db=sinr,
                delay_spread_ns=delay,
                speed_mps=speed,
                scope=scope,
            )
        )
    return conditions


def load_media(path, config):
    inventory = load_yaml(path)
    pools = inventory.get("pools", {})
    expected_videos = int(config["media"]["videos_per_ue"])
    frames_per_scenario = int(config["media"]["frames_per_scenario"])
    segments_per_video = int(config["media"]["segments_per_video"])
    for ue in config["ues"]:
        assets = pools.get(ue, [])
        if len(assets) != expected_videos:
            raise ValueError(
                f"media inventory requires {expected_videos} assets for {ue}; "
                f"found {len(assets)}"
            )
        for asset in assets:
            required = ("media_id", "path", "sha256", "source_frames")
            if not all(asset.get(field) for field in required):
                raise ValueError(f"incomplete media asset for {ue}")
            checksum = str(asset["sha256"]).lower()
            if len(checksum) != 64 or any(c not in string.hexdigits for c in checksum):
                raise ValueError(f"invalid SHA-256 for {ue}")
            required_frames = frames_per_scenario * segments_per_video
            if int(asset["source_frames"]) < required_frames:
                raise ValueError(
                    f"{ue}/{asset['media_id']} has fewer than {required_frames} frames"
                )
    return inventory


def media_assignments(config, inventory, conditions):
    """Assign 333 unique clips per UE, shared by tier and UE-count ablations."""
    frames = int(config["media"]["frames_per_scenario"])
    segments = int(config["media"]["segments_per_video"])
    blocks = [
        (condition["condition_id"], rotation)
        for condition in conditions
        for rotation in range(len(config["application_rotations"]))
    ]
    result = {}
    for ue in config["ues"]:
        slots = [
            (asset, segment * frames)
            for asset in inventory["pools"][ue]
            for segment in range(segments)
        ]
        if len(slots) < len(blocks):
            raise ValueError(f"media inventory has only {len(slots)} clips for {ue}")
        # A UE-specific permutation avoids synchronized identical source traffic.
        random.Random(int(digest([config["campaign_seed"], ue])[:16], 16)).shuffle(slots)
        for block, (asset, source_start_frame) in zip(blocks, slots):
            result[(*block, ue)] = dict(
                media_id=asset["media_id"],
                media_path=asset["path"],
                media_sha256=asset["sha256"],
                source_start_frame=source_start_frame,
                frame_count=frames,
            )
    return result


def validate_config(config):
    if config.get("schema_version") != 1:
        raise ValueError("unsupported campaign schema_version")
    if int(config["duration_s"]) <= 0:
        raise ValueError("duration_s must be positive")
    if config["policies"].get("cuda_priority") != -3:
        raise ValueError("the fixed campaign CUDA priority must be -3")
    for name, members in config["ue_sets"].items():
        unknown = set(members) - set(config["ues"])
        if unknown:
            raise ValueError(f"{name} contains unknown UEs: {sorted(unknown)}")
    for number, rotation in enumerate(config["application_rotations"]):
        if set(rotation) != set(config["ues"]):
            raise ValueError(f"rotation {number} must assign every UE")
        if set(rotation.values()) != set(APPLICATIONS):
            # linux1 repeats pixel1, so four assignments contain three unique apps.
            raise ValueError(f"rotation {number} must cover all applications")
        if rotation["linux1"] != rotation["pixel1"]:
            raise ValueError(f"rotation {number} must keep linux1 with pixel1")
    tiers = config["tiers"]
    if [item["tier"] for item in tiers] != list(range(len(tiers))):
        raise ValueError("tiers must be consecutively numbered from zero")
    for item in tiers:
        if not all(item.get(app) for app in APPLICATIONS):
            raise ValueError(f"tier {item.get('tier')} is incomplete")


def build(config, catalog, inventory):
    validate_config(config)
    candidates = candidate_index(catalog)
    conditions = channel_conditions(config)
    media = media_assignments(config, inventory, conditions)
    scenarios = []
    for condition, rotation_number, tier, (ue_set, active_ues) in product(
        conditions,
        range(len(config["application_rotations"])),
        config["tiers"],
        config["ue_sets"].items(),
    ):
        rotation = config["application_rotations"][rotation_number]
        channel = dict(condition)
        # All ablations of one channel condition reuse its realization.
        channel["realization_seed"] = int(
            digest([config["campaign_seed"], condition["condition_id"]])[:8], 16
        )
        ues = []
        for ue in active_ues:
            app = rotation[ue]
            bitrate = int(tier["bitrate_bps"])
            key = (app, tier[app], bitrate)
            if key not in candidates:
                raise ValueError(f"catalog has no candidate {key}")
            asset = media[(condition["condition_id"], rotation_number, ue)]
            ues.append(
                dict(
                    ue_alias=ue,
                    application=app,
                    candidate_id=candidates[key],
                    bitrate_bps=bitrate,
                    **asset,
                )
            )
        factors = dict(
            ue_set=ue_set,
            application_rotation=rotation_number,
            tier=tier["tier"],
            channel_condition_id=condition["condition_id"],
        )
        scenarios.append(
            dict(
                schema_version=1,
                campaign_id=config["campaign_name"],
                scenario_id="scn-" + digest(factors)[:20],
                factors=factors,
                duration_s=config["duration_s"],
                fps=config["fps"],
                gop_frames=config["gop_frames"],
                policies=config["policies"],
                channel=channel,
                ues=ues,
            )
        )
    if len({item["scenario_id"] for item in scenarios}) != len(scenarios):
        raise RuntimeError("scenario ID collision")
    expected = config["execution"].get("expected_scenarios")
    if expected is not None and len(scenarios) != int(expected):
        raise ValueError(f"generated {len(scenarios)} scenarios; expected {expected}")
    if config["execution"].get("randomize_order"):
        random.Random(config["campaign_seed"]).shuffle(scenarios)
    for index, scenario in enumerate(scenarios):
        scenario["execution_index"] = index
    return scenarios


def counts(scenarios, key):
    result = {}
    for scenario in scenarios:
        value = str(scenario["factors"][key])
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--catalog", default="config/tasks.yaml")
    parser.add_argument("--media-inventory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = load_yaml(args.campaign)
    catalog = load_yaml(args.catalog)
    inventory = load_media(args.media_inventory, config)
    scenarios = build(config, catalog, inventory)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "scenarios.jsonl"
    manifest.write_text("".join(canonical(item) + "\n" for item in scenarios))
    lock = {
        "schema_version": 1,
        "campaign_id": config["campaign_name"],
        "scenario_count": len(scenarios),
        "campaign_sha256": file_digest(args.campaign),
        "catalog_sha256": file_digest(args.catalog),
        "media_inventory_sha256": file_digest(args.media_inventory),
        "scenarios_sha256": file_digest(manifest),
        "counts": {
            "ue_set": counts(scenarios, "ue_set"),
            "application_rotation": counts(scenarios, "application_rotation"),
            "tier": counts(scenarios, "tier"),
        },
    }
    (output / "manifest.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(f"wrote {len(scenarios)} scenarios to {manifest}")


if __name__ == "__main__":
    main()

