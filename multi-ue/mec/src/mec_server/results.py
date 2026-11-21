"""A prepared frame result: reusable wire text and read-only routing metadata."""
from collections.abc import Mapping
from types import MappingProxyType
from mec_contracts.wire import dumps, loads


class SerializedResult(Mapping):
    def __init__(self, text, fields):
        self.text = text
        # Routing/logging never needs to retain the potentially large payload.
        self._fields = MappingProxyType({k: v for k, v in fields.items()
                                         if k not in ("payload", "stage_times")})

    @classmethod
    def prepare(cls, message):
        wire = {k: v for k, v in message.items() if k != "stage_times"}
        return cls(dumps(wire), wire)

    @classmethod
    def from_storage(cls, text):
        fields = loads(text)
        # Old databases may contain stage timings. Strip them during legacy replay.
        if "stage_times" in fields:
            return cls.prepare(fields)
        return cls(text, fields)

    def __getitem__(self, key):
        return self._fields[key]

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)
