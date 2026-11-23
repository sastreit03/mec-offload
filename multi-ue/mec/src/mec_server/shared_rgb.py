"""Bounded decoded RGB storage. Local leases never cross multiprocessing IPC.

The producer owns/unlinks each region. Workers receive only a descriptor and
acknowledge consumption; Workers retains a local lease until that acknowledgement
or confirmed process exit. A separate lease protects each preview encoder.
"""
from dataclasses import asdict, dataclass
from multiprocessing.shared_memory import SharedMemory
from threading import RLock
import struct
import os

HEADER_BYTES = 64

import numpy as np


@dataclass(frozen=True)
class RGBDescriptor:
    region: str
    offset: int
    slot: int
    generation: int
    shape: tuple
    row_stride: int
    slot_bytes: int


class RGBLease:
    def __init__(self, pool, descriptor):
        self.pool = pool
        self.descriptor = descriptor
        self.released = False

    def retain(self):
        with self.pool.lock:
            if self.released:
                raise RuntimeError("released RGB lease")
            self.pool.references[self.descriptor.slot] += 1
            return RGBLease(self.pool, self.descriptor)

    def array(self):
        with self.pool.lock:
            if self.released:
                raise RuntimeError("released RGB lease")
            return rgb_view(self.pool.region, self.descriptor)

    def release(self):
        with self.pool.lock:
            if not self.released:
                self.released = True
                self.pool.references[self.descriptor.slot] -= 1
                self.pool._dispose_if_drained()

    def wire(self):
        if self.released:
            raise RuntimeError("released RGB lease")
        return asdict(self.descriptor)


def release_rgb(rgb):
    if isinstance(rgb, RGBLease):
        rgb.release()


def rgb_view(region, descriptor):
    d = descriptor
    if (len(d.shape) != 3 or d.shape[2] != 3 or min(d.shape) <= 0
            or d.row_stride < d.shape[1]*3 or d.slot < 0 or d.generation < 1
            or d.slot_bytes <= HEADER_BYTES or d.offset != d.slot*d.slot_bytes+HEADER_BYTES
            or (d.shape[0]-1)*d.row_stride + d.shape[1]*3 > d.slot_bytes-HEADER_BYTES
            or (d.slot+1)*d.slot_bytes > region.size):
        raise ValueError("invalid shared RGB descriptor")
    if struct.unpack_from("=Q", region.buf, d.slot*d.slot_bytes)[0] != d.generation:
        raise ValueError("stale shared RGB generation")
    result = np.ndarray(tuple(d.shape), dtype=np.uint8, buffer=region.buf,
                        offset=d.offset, strides=(d.row_stride, 3, 1))
    result.flags.writeable = False
    return result


class RGBMapping:
    """Worker-local mapping; close only after all views have been dropped."""
    def __init__(self, wire):
        self.descriptor = RGBDescriptor(**wire)
        self.region = SharedMemory(name=self.descriptor.region)
        try:
            self.image = rgb_view(self.region, self.descriptor)
        except BaseException:
            self.region.close()
            raise

    def close(self):
        self.image = None
        self.region.close()


class RGBPool:
    def __init__(self, slots, max_bytes):
        if slots <= 0 or max_bytes <= 0:
            raise ValueError("positive shared RGB limits required")
        self.slots = slots
        self.slot_bytes = ((max_bytes+HEADER_BYTES+4095)//4096)*4096
        self.max_bytes = max_bytes
        self.region = None  # Receiver.start reserves storage before enabling media.
        self.references = [0]*slots
        self.generations = [0]*slots
        self.lock = RLock()
        self.closed = False
        self.copies = self.exhaustions = self.peak = 0

    def allocate(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("closed shared RGB pool")
            if self.region is None:
                region = SharedMemory(create=True, size=self.slots*self.slot_bytes)
                try:
                    # Linux tmpfs otherwise faults with SIGBUS on first write
                    # when /dev/shm is full. Reserve backing before publishing.
                    os.posix_fallocate(region._fd, 0, region.size)
                except BaseException:
                    region.close()
                    region.unlink()
                    raise
                self.region = region

    def registration(self):
        """Region metadata for worker preparation, independent of frame slots."""
        with self.lock:
            self.allocate()
            return dict(region=self.region.name, size=self.region.size)

    def copy(self, image):
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("expected HWC uint8 RGB")
        if image.nbytes > self.max_bytes or min(image.shape) <= 0:
            raise ValueError("decoded_byte_budget")
        with self.lock:
            if self.closed:
                return None
            try:
                slot = self.references.index(0)
            except ValueError:
                self.exhaustions += 1
                return None
            self.allocate()
            self.generations[slot] += 1
            d = RGBDescriptor(self.region.name, slot*self.slot_bytes+HEADER_BYTES, slot,
                              self.generations[slot], image.shape, image.shape[1]*3,
                              self.slot_bytes)
            target = np.ndarray(image.shape, np.uint8, buffer=self.region.buf,
                                offset=d.offset)
            np.copyto(target, image)
            del target
            struct.pack_into("=Q", self.region.buf, slot*self.slot_bytes, d.generation)
            self.references[slot] = 1
            self.copies += 1
            self.peak = max(self.peak, sum(n > 0 for n in self.references))
            return RGBLease(self, d)

    def stats(self):
        with self.lock:
            return dict(slots=self.slots, capacity_bytes=self.slots*self.slot_bytes, in_use=sum(n > 0 for n in self.references),
                        references=sum(self.references), peak=self.peak,
                        copies=self.copies, exhaustions=self.exhaustions,
                        closed=self.closed, mapped=self.region is not None)

    def _dispose_if_drained(self):
        if self.closed and not any(self.references) and self.region is not None:
            self.region.close()
            self.region.unlink()
            self.region = None

    def close(self):
        with self.lock:
            self.closed = True
            self._dispose_if_drained()
