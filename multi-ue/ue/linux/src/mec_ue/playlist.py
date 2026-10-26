from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from pathlib import Path


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v"}


@dataclass(frozen=True)
class Selection:
    path: Path
    cycle: int
    position: int


class Playlist:
    """Seeded shuffle: visit every prerecorded video once before reshuffling."""

    def __init__(self, directory, seed=None):
        root = Path(directory)
        self.files = sorted(
            p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
        )
        if not self.files:
            raise ValueError(f"no supported videos in {root}")
        self.seed = secrets.randbits(63) if seed is None else int(seed)
        self.random = random.Random(self.seed)
        self.order = []
        self.index = 0
        self.cycle = -1
        self.last = None

    def next(self):
        if self.index >= len(self.order):
            self.order = self.files[:]
            self.random.shuffle(self.order)
            # Avoid an immediate repeat across cycles when another choice exists.
            if len(self.order) > 1 and self.order[0] == self.last:
                self.order[0], self.order[1] = self.order[1], self.order[0]
            self.index = 0
            self.cycle += 1
        value = Selection(self.order[self.index], self.cycle, self.index)
        self.index += 1
        self.last = value.path
        return value
