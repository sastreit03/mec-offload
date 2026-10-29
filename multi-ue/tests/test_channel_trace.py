import json
from pathlib import Path

import numpy as np
import pytest

from channel_trace import validate_trace_folder, write_entries


def make_trace(folder: Path, maximum_index=7):
    taps = np.zeros((28, 4), dtype=np.complex64)
    taps[:, 0] = 1.0
    indices = np.zeros((28, 4), dtype=np.uint16)
    indices[:, 0] = maximum_index
    trace_hash = write_entries(
        folder / "cirs.bin", np.ones(28), taps, indices
    )
    (folder / "config.json").write_text(json.dumps({
        "channel_emulation": {
            "num_taps": 4,
            "num_cirs": 28,
            "sigma_scaling": 0.1,
            "sigma_max": 1.0,
        },
        "aetheredge": {
            "condition_id": "tdl-c-sinr10-delay-spread100-speed1",
            "trace_sha256": trace_hash,
            "reads_per_slot": 2,
            "num_symbols_per_slot": 14,
        },
    }))


def test_trace_validation_accepts_complete_file(tmp_path):
    make_trace(tmp_path)
    result = validate_trace_folder(
        tmp_path,
        condition_id="tdl-c-sinr10-delay-spread100-speed1",
        num_symbols=14,
        num_taps=4,
    )
    assert result["num_slots"] == 2
    assert result["max_tap_index"] == 7


def test_trace_validation_rejects_delay_index_256(tmp_path):
    make_trace(tmp_path, maximum_index=256)
    with pytest.raises(ValueError, match="exceeds 255"):
        validate_trace_folder(tmp_path)
