"""M59 fixed descriptor and RFC 8285 two-byte extension; canonical Annex-B AU bytes."""

from __future__ import annotations
from dataclasses import dataclass, asdict
import struct
import uuid

DESCRIPTOR = struct.Struct("!BBH16sQQQIIB3xQQQQQ")
assert DESCRIPTOR.size == 96


@dataclass(frozen=True)
class Descriptor:
    video_id: str
    media_epoch: int
    frame_id: int
    gop_id: int
    config_version: int
    plan_version: int
    frame_type: int
    encoded_bytes: int
    capture_mono_ns: int = 0
    tx_mono_ns: int = 0
    capture_unix_ns: int = 0
    tx_unix_ns: int = 0
    flags: int = 0

    def pack(self):
        if (
            not self.media_epoch
            or self.encoded_bytes <= 0
            or self.frame_type not in range(4)
        ):
            raise ValueError("invalid frame descriptor")
        if self.flags & ~15:
            raise ValueError("reserved flags")
        times = (
            self.capture_mono_ns,
            self.tx_mono_ns,
            self.capture_unix_ns,
            self.tx_unix_ns,
        )
        if any(not (self.flags & (1 << i)) and t for i, t in enumerate(times)):
            raise ValueError("timestamp set without validity flag")
        return DESCRIPTOR.pack(
            1,
            self.flags,
            0,
            uuid.UUID(self.video_id).bytes,
            self.media_epoch,
            self.frame_id,
            self.gop_id,
            self.config_version,
            self.plan_version,
            self.frame_type,
            self.encoded_bytes,
            *times,
        )

    @classmethod
    def unpack(cls, raw):
        if len(raw) != 96 or raw[2:4] != b"\0\0":
            raise ValueError("descriptor length/reserved bytes")
        values = DESCRIPTOR.unpack(raw)
        (
            schema,
            flags,
            reserved,
            video,
            epoch,
            frame,
            gop,
            config,
            plan,
            kind,
            size,
            *times,
        ) = values
        if schema != 1 or reserved or raw[53:56] != b"\0\0\0":
            raise ValueError("descriptor version/reserved bytes")
        d = cls(
            str(uuid.UUID(bytes=video)),
            epoch,
            frame,
            gop,
            config,
            plan,
            kind,
            size,
            *times,
            flags,
        )
        d.pack()
        return d

    def record(self):
        return asdict(self)


@dataclass(frozen=True)
class Packet:
    sequence: int
    timestamp: int
    ssrc: int
    marker: bool
    descriptor: Descriptor
    payload: bytes


def parse_packet(raw, extension_id=1):
    if len(raw) < 12 or raw[0] >> 6 != 2 or not raw[0] & 16:
        raise ValueError("RTP v2 with descriptor extension required")
    off = 12 + 4 * (raw[0] & 15)
    if len(raw) < off + 4:
        raise ValueError("truncated RTP extension")
    profile, words = struct.unpack_from("!HH", raw, off)
    if profile & 0xFFF0 != 0x1000:
        raise ValueError("two-byte RFC8285 extension required")
    end = off + 4 + words * 4
    if end > len(raw):
        raise ValueError("truncated extension")
    pos, desc = off + 4, None
    while pos < end:
        ident = raw[pos]
        pos += 1
        if ident == 0:
            continue
        if pos >= end:
            raise ValueError("truncated element")
        size = raw[pos]
        pos += 1
        if pos + size > end:
            raise ValueError("element outside extension")
        if ident == extension_id:
            if desc is not None:
                raise ValueError("duplicate descriptor element")
            desc = Descriptor.unpack(raw[pos : pos + size])
        pos += size
    if desc is None:
        raise ValueError("descriptor missing")
    stop = len(raw)
    if raw[0] & 32:
        padding = raw[-1]
        if padding < 1 or padding > stop - end:
            raise ValueError("bad RTP padding")
        stop -= padding
    if stop <= end:
        raise ValueError("empty payload")
    seq, ts, ssrc = struct.unpack_from("!HII", raw, 2)
    return Packet(seq, ts, ssrc, bool(raw[1] & 128), desc, raw[end:stop])


def split_annexb(data):
    import re

    nals = [v for v in re.split(b"\x00\x00(?:\x00)?\x01", data) if v]
    if not nals:
        raise ValueError("empty AU")
    return nals


def canonical_au(nals):
    return b"".join(b"\0\0\0\1" + n for n in nals)


def packetize(nals, descriptor, sequence, timestamp, ssrc, mtu=1200, extension_id=1):
    if not 1 <= extension_id <= 255 or mtu < 256:
        raise ValueError("invalid MTU/extension ID")
    ext = bytes([extension_id, 96]) + descriptor.pack() + b"\0\0"
    overhead = 12 + 4 + len(ext)
    limit = mtu - overhead
    if descriptor.encoded_bytes != len(canonical_au(nals)):
        raise ValueError("AU size differs from descriptor")
    for index, nal in enumerate(nals):
        if not nal or not 1 <= nal[0] & 31 <= 23:
            raise ValueError("unsupported NAL")
        payloads = [nal]
        if len(nal) > limit:
            chunks = [nal[i : i + limit - 2] for i in range(1, len(nal), limit - 2)]
            payloads = [
                bytes(
                    [
                        (nal[0] & 224) | 28,
                        (nal[0] & 31)
                        | (128 if i == 0 else 0)
                        | (64 if i == len(chunks) - 1 else 0),
                    ]
                )
                + chunk
                for i, chunk in enumerate(chunks)
            ]
        for j, payload in enumerate(payloads):
            marker = index == len(nals) - 1 and j == len(payloads) - 1
            yield (
                struct.pack(
                    "!BBHIIHH",
                    0x90,
                    96 | (128 if marker else 0),
                    sequence & 65535,
                    timestamp & 0xFFFFFFFF,
                    ssrc,
                    0x1000,
                    len(ext) // 4,
                )
                + ext
                + payload
            )
            sequence += 1


class Assembly:
    """Unique encoded-byte progress from payloads; marker alone never proves completion."""

    def __init__(self, descriptor, max_bytes=8 * 1024 * 1024):
        if descriptor.encoded_bytes > max_bytes:
            raise ValueError("encoded AU exceeds configured receiver budget")
        self.descriptor = descriptor
        self.packets = {}
        self.marker = None
        self.unique_bytes = 0

    @property
    def complete(self):
        return (
            self.marker is not None
            and self.unique_bytes == self.descriptor.encoded_bytes
        )

    def add(self, p):
        if p.descriptor != self.descriptor:
            raise ValueError("conflicting repeated descriptor")
        if p.sequence in self.packets:
            if self.packets[p.sequence] != p.payload:
                raise ValueError("conflicting RTP sequence")
            return self.complete
        data = p.payload
        kind = data[0] & 31
        if kind == 28:
            if len(data) < 3:
                raise ValueError("short FU-A")
            amount = len(data) - 2 + (5 if data[1] & 128 else 0)
        elif 1 <= kind <= 23:
            amount = len(data) + 4
        else:
            raise ValueError("only single NAL and FU-A supported")
        if self.unique_bytes + amount > self.descriptor.encoded_bytes:
            raise ValueError("payload exceeds advertised AU size")
        self.packets[p.sequence] = data
        self.unique_bytes += amount
        if p.marker:
            self.marker = p.sequence
        return (
            self.marker is not None
            and self.unique_bytes == self.descriptor.encoded_bytes
        )
