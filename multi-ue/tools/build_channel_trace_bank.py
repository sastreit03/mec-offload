#!/usr/bin/env python3
"""Build deterministic SRK file traces for an Aetheredge campaign.

TDL/CDL coefficients come from Sionna PHY's 3GPP TR 38.901 models. The
resulting sparse, symbol-static CIRs use the packed ABI consumed by SRK.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import sys
from pathlib import Path

import numpy as np
import yaml

from channel_trace import validate_trace_folder, write_entries
from generate_campaign_manifest import channel_conditions, digest


def radio_parameters(args) -> dict:
    saved = {}
    if args.loader_config:
        saved = json.loads(Path(args.loader_config).read_text())
    result = {
        "fft_size": args.fft_size or saved.get("fft_size"),
        "subcarrier_spacing_hz": args.scs_hz or saved.get("subcarrier_spacing"),
        "carrier_frequency_hz": args.carrier_frequency_hz or saved.get("frequency"),
        "num_symbols_per_slot": args.num_symbols or saved.get("num_ofdm_symbols_per_slot"),
        "num_taps": args.num_taps or saved.get("num_taps"),
    }
    missing = [name for name, value in result.items() if value is None]
    if missing:
        raise ValueError("missing loader radio parameters: " + ", ".join(missing))
    result = {name: int(value) if name in {"fft_size", "num_symbols_per_slot", "num_taps"}
              else float(value) for name, value in result.items()}
    result["sample_rate_hz"] = result["fft_size"] * result["subcarrier_spacing_hz"]
    slots_per_second = result["subcarrier_spacing_hz"] / 15000.0 * 1000.0
    result["symbol_rate_hz"] = slots_per_second * result["num_symbols_per_slot"]
    return result


def sionna_coefficients(condition, radio, time_steps, seed):
    try:
        import tensorflow as tf
        import sionna
        from sionna.phy import config as sionna_config
        from sionna.phy.channel.tr38901 import CDL, TDL, PanelArray
    except ImportError as exc:
        raise RuntimeError(
            "Sionna PHY and TensorFlow are required for TDL/CDL generation"
        ) from exc

    tf.random.set_seed(seed)
    sionna_config.seed = seed
    model = condition["model"].split("-", 1)[1]
    kwargs = dict(
        model=model,
        delay_spread=float(condition["delay_spread_ns"]) * 1e-9,
        carrier_frequency=radio["carrier_frequency_hz"],
        min_speed=float(condition["speed_mps"]),
        max_speed=float(condition["speed_mps"]),
    )
    if condition["model"].startswith("TDL-"):
        channel = TDL(num_rx_ant=1, num_tx_ant=1, **kwargs)
    else:
        array = PanelArray(
            num_rows_per_panel=1,
            num_cols_per_panel=1,
            polarization="single",
            polarization_type="V",
            antenna_pattern="omni",
            carrier_frequency=radio["carrier_frequency_hz"],
        )
        channel = CDL(ut_array=array, bs_array=array, direction="uplink", **kwargs)
    h, tau = channel(
        batch_size=1,
        num_time_steps=time_steps,
        sampling_frequency=radio["symbol_rate_hz"],
    )
    coefficients = np.asarray(h)[0, 0, 0, 0, 0].T.astype(np.complex64)
    delays = np.asarray(tau)[0, 0, 0].astype(np.float64)
    return coefficients, delays, {
        "sionna_version": getattr(sionna, "__version__", "unknown"),
        "tensorflow_version": tf.__version__,
    }


def sparse_entries(coefficients, delays, radio):
    delay_indices = np.rint(delays * radio["sample_rate_hz"]).astype(np.int64)
    if np.any(delay_indices < 0) or np.any(delay_indices >= 256):
        raise ValueError(
            f"quantized tap indices outside 0..255: "
            f"{int(delay_indices.min())}..{int(delay_indices.max())}"
        )
    unique = np.unique(delay_indices)
    combined = np.stack(
        [coefficients[:, delay_indices == index].sum(axis=1) for index in unique],
        axis=1,
    )
    energy = np.mean(np.abs(combined) ** 2, axis=0)
    order = np.argsort(energy)[::-1]
    retained = order[: radio["num_taps"]]
    retained_energy = float(energy[retained].sum() / max(energy.sum(), 1e-30))
    combined = combined[:, retained]
    unique = unique[retained]

    rows = combined.shape[0]
    taps = np.zeros((rows, radio["num_taps"]), dtype=np.complex64)
    indices = np.zeros((rows, radio["num_taps"]), dtype=np.uint16)
    taps[:, : combined.shape[1]] = combined
    indices[:, : combined.shape[1]] = unique.astype(np.uint16)
    norms = np.sqrt(np.sum(np.abs(taps) ** 2, axis=1)).astype(np.float32)
    if np.any(~np.isfinite(norms)) or np.any(norms <= 0):
        raise ValueError("Sionna produced a non-finite or zero channel norm")
    taps /= norms[:, None]
    return norms, taps, indices, retained_energy


def build_trace(condition, radio, duration_s, seed, folder):
    symbols = radio["num_symbols_per_slot"]
    if condition["model"] == "AWGN":
        time_steps = symbols
        coefficients = np.ones((time_steps, 1), dtype=np.complex64)
        delays = np.zeros(1, dtype=np.float64)
        versions = {"sionna_version": None, "tensorflow_version": None}
    else:
        num_slots = math.ceil(duration_s * radio["symbol_rate_hz"] / symbols)
        time_steps = num_slots * symbols
        coefficients, delays, versions = sionna_coefficients(
            condition, radio, time_steps, seed
        )
    norms, taps, indices, retained_energy = sparse_entries(
        coefficients, delays, radio
    )
    trace_hash = write_entries(folder / "cirs.bin", norms, taps, indices)
    noise_std = 10.0 ** (-float(condition["sinr_db"]) / 20.0)
    config = {
        "channel_emulation": {
            "num_taps": radio["num_taps"],
            "num_cirs": int(time_steps),
            "sigma_scaling": noise_std,
            "sigma_max": 1.0,
        },
        "aetheredge": {
            **condition,
            "trace_sha256": trace_hash,
            "realization_seed": seed,
            # nr-ru applies the same slot once on RX and once on TX.
            "reads_per_slot": 2,
            "num_symbols_per_slot": symbols,
            "duration_s": duration_s if condition["model"] != "AWGN" else None,
            "fft_size": radio["fft_size"],
            "subcarrier_spacing_hz": radio["subcarrier_spacing_hz"],
            "carrier_frequency_hz": radio["carrier_frequency_hz"],
            "sample_rate_hz": radio["sample_rate_hz"],
            "symbol_rate_hz": radio["symbol_rate_hz"],
            "retained_energy_ratio": retained_energy,
            **versions,
        },
    }
    (folder / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    return validate_trace_folder(
        folder,
        condition_id=condition["condition_id"],
        num_symbols=symbols,
        num_taps=radio["num_taps"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--loader-config")
    parser.add_argument("--fft-size", type=int)
    parser.add_argument("--scs-hz", type=float)
    parser.add_argument("--carrier-frequency-hz", type=float)
    parser.add_argument("--num-symbols", type=int)
    parser.add_argument("--num-taps", type=int)
    parser.add_argument("--condition-id", action="append", default=[])
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    campaign_path = Path(args.campaign)
    campaign = yaml.safe_load(campaign_path.read_text())
    radio = radio_parameters(args)
    conditions = channel_conditions(campaign)
    if args.condition_id:
        requested = set(args.condition_id)
        conditions = [c for c in conditions if c["condition_id"] in requested]
        missing = requested - {c["condition_id"] for c in conditions}
        if missing:
            raise ValueError(f"unknown condition IDs: {sorted(missing)}")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for number, condition in enumerate(conditions, 1):
        target = output / condition["condition_id"]
        if target.exists():
            if not args.overwrite:
                records.append(validate_trace_folder(
                    target,
                    condition_id=condition["condition_id"],
                    num_symbols=radio["num_symbols_per_slot"],
                    num_taps=radio["num_taps"],
                ))
                print(f"[{number}/{len(conditions)}] reused {condition['condition_id']}")
                continue
            shutil.rmtree(target)
        temporary = output / ("." + condition["condition_id"] + ".tmp")
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir()
        seed = int(digest([campaign["campaign_seed"], condition["condition_id"]])[:8], 16)
        record = build_trace(
            condition, radio, float(campaign["duration_s"]), seed, temporary
        )
        temporary.rename(target)
        records.append(record)
        print(f"[{number}/{len(conditions)}] wrote {condition['condition_id']}")

    manifest = {
        "schema_version": 1,
        "campaign": campaign["campaign_name"],
        "campaign_sha256": __import__("hashlib").sha256(campaign_path.read_bytes()).hexdigest(),
        "radio": radio,
        "generator": {
            "python": platform.python_version(),
            "numpy": np.__version__,
        },
        "traces": records,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"validated {len(records)} traces in {output}")


if __name__ == "__main__":
    main()
