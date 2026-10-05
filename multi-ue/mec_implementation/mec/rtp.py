"""RTP wire layout shared by MEC and UE. No per-frame control message is needed.

RFC 8285 two-byte extension ID=1, profile=0x1000:
version:u8, task UUID:16, media token:16, frame_id:u64,
capture_unix_ns:u64, tx_start_unix_ns:u64 (network byte order).
PT 96 = H.264 (RFC 6184). PT 97 = our test JPEG fragment format, NOT RFC 2435.
"""
import re
import struct
from dataclasses import dataclass
from uuid import UUID

META = struct.Struct("!B16s16sQQQ")
FRAGMENT = struct.Struct("!II")  # JPEG byte offset and total encoded size.
EXTENSION_ID = 1


@dataclass(frozen=True)
class Header:
    task_id: str
    token: bytes
    frame_id: int
    capture_unix_ns: int
    tx_unix_ns: int


def packet(header: Header, payload: bytes, *, sequence: int, timestamp: int,
           ssrc: int, marker: bool, payload_type: int) -> bytes:
    extension = bytes([EXTENSION_ID, META.size]) + META.pack(
        1, UUID(header.task_id).bytes, header.token, header.frame_id,
        header.capture_unix_ns, header.tx_unix_ns)
    extension += b"\x00" * (-len(extension) % 4)
    return (struct.pack("!BBHII", 0x90, (0x80 if marker else 0) | payload_type,
                        sequence & 65535, timestamp & 0xffffffff, ssrc)
            + struct.pack("!HH", 0x1000, len(extension) // 4) + extension + payload)


def parse(data: bytes):
    if len(data) < 16 or data[0] >> 6 != 2 or not data[0] & 0x10:
        raise ValueError("RTP metadata extension required")
    offset = 12 + (data[0] & 15) * 4
    if offset + 4 > len(data):
        raise ValueError("Truncated RTP")
    profile, words = struct.unpack_from("!HH", data, offset)
    if profile != 0x1000:
        raise ValueError("Unknown RTP extension profile")
    start, end = offset + 4, offset + 4 + words * 4
    if end > len(data):
        raise ValueError("Truncated RTP extensions")
    found = None
    cursor = start
    while cursor < end:
        ident = data[cursor]
        cursor += 1
        if ident == 0:
            continue
        if cursor >= end:
            raise ValueError("Truncated extension entry")
        size = data[cursor]
        cursor += 1
        if cursor + size > end:
            raise ValueError("Truncated extension data")
        if ident == EXTENSION_ID:
            if size != META.size:
                raise ValueError("Wrong metadata size")
            version, task, token, fid, capture, tx = META.unpack_from(data, cursor)
            if version != 1:
                raise ValueError("Unknown metadata version")
            found = Header(str(UUID(bytes=task)), token, fid, capture, tx)
        cursor += size
    if found is None:
        raise ValueError("No frame metadata")
    tail = len(data)
    if data[0] & 0x20:
        padding = data[-1]
        if not padding or padding > tail - end:
            raise ValueError("Invalid RTP padding")
        tail -= padding
    seq, timestamp, ssrc = struct.unpack_from("!HII", data, 2)
    return found, data[end:tail], seq, timestamp, ssrc, bool(data[1] & 128), data[1] & 127


def payloads(encoded: bytes, codec: str, limit: int):
    if codec == "jpeg":
        for offset in range(0, len(encoded), limit - FRAGMENT.size):
            yield FRAGMENT.pack(offset, len(encoded)) + encoded[offset:offset + limit - FRAGMENT.size]
        return
    # Annex-B access unit, fragmented with RFC 6184 FU-A when needed.
    nals = [x for x in re.split(b"\x00\x00\x00?\x01", encoded) if x]
    if not nals:
        raise ValueError("Encoder produced no H.264 NALs")
    for nal in nals:
        if len(nal) <= limit:
            yield nal
        else:
            indicator = (nal[0] & 0xe0) | 28
            chunks = [nal[i:i + limit - 2] for i in range(1, len(nal), limit - 2)]
            for index, chunk in enumerate(chunks):
                flag = (128 if index == 0 else 0) | (64 if index == len(chunks) - 1 else 0)
                yield bytes([indicator, flag | (nal[0] & 31)]) + chunk


def packetize(header, encoded, codec, *, sequence, timestamp, ssrc, mtu=1200):
    overhead = len(packet(header, b"", sequence=0, timestamp=0, ssrc=0, marker=False, payload_type=96))
    parts = list(payloads(encoded, codec, mtu - overhead))
    for index, payload in enumerate(parts):
        yield packet(header, payload, sequence=sequence + index, timestamp=timestamp,
                     ssrc=ssrc, marker=index == len(parts) - 1, payload_type=96 if codec == "h264" else 97)
