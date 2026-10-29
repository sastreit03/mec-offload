#!/usr/bin/env python3
"""Shared SRK trace-bank format and validation helpers for Aetheredge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


MAX_TAP_DELAY_SAMPLES = 256  # Valid packed indices are 0 through 255.


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_dtype(num_taps: int) -> np.dtype:
    raw_size = 4 + num_taps * 8 + num_taps * 2
    itemsize = (raw_size + 3) & ~3
    return np.dtype(
        {
            "names": ("norm", "taps", "indices"),
            "formats": ("<f4", ("<f4", 2 * num_taps), ("<u2", num_taps)),
            "offsets": (0, 4, 4 + num_taps * 8),
            "itemsize": itemsize,
        }
    )


def validate_trace_folder(
    folder: Path,
    *,
    condition_id: str | None = None,
    num_symbols: int | None = None,
    num_taps: int | None = None,
) -> dict:
    """Validate metadata, exact binary size, hash, and every packed CIR entry."""
    config_path = folder / "config.json"
    binary_path = folder / "cirs.bin"
    config = json.loads(config_path.read_text())
    channel = config["channel_emulation"]
    metadata = config["aetheredge"]
    taps = int(channel["num_taps"])
    cirs = int(channel["num_cirs"])
    symbols = int(metadata["num_symbols_per_slot"])
    if condition_id is not None and metadata["condition_id"] != condition_id:
        raise ValueError(f"{folder}: condition ID mismatch")
    if num_symbols is not None and symbols != num_symbols:
        raise ValueError(f"{folder}: expected {num_symbols} symbols, found {symbols}")
    if num_taps is not None and taps != num_taps:
        raise ValueError(f"{folder}: expected {num_taps} taps, found {taps}")
    if taps <= 0 or taps > MAX_TAP_DELAY_SAMPLES:
        raise ValueError(f"{folder}: num_taps must be in [1, 256]")
    if cirs <= 0 or cirs % symbols:
        raise ValueError(f"{folder}: num_cirs must contain complete slots")
    if int(metadata.get("reads_per_slot", 0)) not in (1, 2):
        raise ValueError(f"{folder}: reads_per_slot must be 1 or 2")

    dtype = entry_dtype(taps)
    expected_bytes = cirs * dtype.itemsize
    if binary_path.stat().st_size != expected_bytes:
        raise ValueError(
            f"{folder}: expected {expected_bytes} binary bytes, "
            f"found {binary_path.stat().st_size}"
        )
    actual_hash = sha256(binary_path)
    if actual_hash.lower() != str(metadata["trace_sha256"]).lower():
        raise ValueError(f"{folder}: cirs.bin SHA-256 mismatch")

    entries = np.memmap(binary_path, mode="r", dtype=dtype, shape=(cirs,))
    if not np.isfinite(entries["norm"]).all() or np.any(entries["norm"] <= 0):
        raise ValueError(f"{folder}: norms must be finite and positive")
    if not np.isfinite(entries["taps"]).all():
        raise ValueError(f"{folder}: taps must be finite")
    maximum_index = int(entries["indices"].max(initial=0))
    if maximum_index >= MAX_TAP_DELAY_SAMPLES:
        raise ValueError(f"{folder}: tap index {maximum_index} exceeds 255")
    if float(channel["sigma_scaling"]) < 0 or float(channel["sigma_max"]) < 0:
        raise ValueError(f"{folder}: noise parameters must be nonnegative")
    return {
        "condition_id": metadata["condition_id"],
        "trace_sha256": actual_hash,
        "num_taps": taps,
        "num_cirs": cirs,
        "num_slots": cirs // symbols,
        "max_tap_index": maximum_index,
        "reads_per_slot": int(metadata["reads_per_slot"]),
        "bytes": expected_bytes,
    }


def write_entries(path: Path, norms, taps, indices) -> str:
    """Write already normalized sparse taps using SRK's packed little-endian ABI."""
    norms = np.asarray(norms, dtype=np.float32)
    taps = np.asarray(taps, dtype=np.complex64)
    indices = np.asarray(indices, dtype=np.uint16)
    if taps.ndim != 2 or indices.shape != taps.shape or norms.shape != taps.shape[:1]:
        raise ValueError("inconsistent norm/tap/index shapes")
    dtype = entry_dtype(taps.shape[1])
    entries = np.zeros(taps.shape[0], dtype=dtype)
    entries["norm"] = norms
    entries["taps"][:, 0::2] = taps.real
    entries["taps"][:, 1::2] = taps.imag
    entries["indices"] = indices
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(entries.tobytes())
    return sha256(path)
