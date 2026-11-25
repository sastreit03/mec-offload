"""A prepared frame result: reusable wire text and read-only routing metadata."""
import time
from collections.abc import Mapping
from types import MappingProxyType
from mec_contracts.wire import dumps, loads


class SerializedResult(Mapping):
    def __init__(self, text, fields):
        self.text = text
        self.timing = {}
        # Routing/logging never needs to retain the potentially large payload.
        self._fields = MappingProxyType({k: v for k, v in fields.items()
                                         if k not in ("payload", "stage_times")})

    @classmethod
    def prepare(cls, message):
        begin, cpu = time.monotonic_ns(), time.thread_time_ns()
        # Put the potentially large payload after every receipt field on the wire.
        wire = {k: v for k, v in message.items() if k not in ("stage_times", "payload")}
        if "payload" in message:
            wire["payload"] = message["payload"]
        result = cls(dumps(wire), wire)
        end = time.monotonic_ns()
        result.timing.update(serialization_start_ns=begin, serialization_end_ns=end,
                             serialization_s=(end-begin)/1e9,
                             serialization_thread_s=(time.thread_time_ns()-cpu)/1e9)
        return result

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
