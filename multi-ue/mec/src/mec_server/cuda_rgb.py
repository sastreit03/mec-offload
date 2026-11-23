"""Worker-local registered mappings. Each GPU reader holds a counted lease."""
import ctypes
import os
from collections import OrderedDict
from pathlib import Path
from multiprocessing.shared_memory import SharedMemory
from threading import RLock
from .shared_rgb import RGBMapping


class NativeRGB:
    def __init__(self):
        path = os.getenv('MEC_RGB_CUDA_LIBRARY', str(Path(__file__).with_name('shared_preprocess.so')))
        self.lib = lib = ctypes.CDLL(path)
        lib.error_text.argtypes = [ctypes.c_int]
        lib.error_text.restype = ctypes.c_char_p
        lib.register_region.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.device_pointer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.unregister_region.argtypes = [ctypes.c_void_p]
        lib.preprocess.argtypes = [ctypes.c_uint64]*2 + [ctypes.c_int]*10 + [ctypes.c_uint64]

    def check(self, code):
        if code:
            raise RuntimeError(f'mapped RGB CUDA {code}: {self.lib.error_text(code).decode()}')


class GPUInput:
    def __init__(self, cache, entry, mapping):
        self.cache, self.entry, self.mapping = cache, entry, mapping
        self.image = mapping.image
        self.descriptor = mapping.descriptor
        self.pointer = entry['pointer']+self.descriptor.offset
        self.closed = False

    def close(self):
        # Caller must first observe the event following the last GPU RGB read.
        with self.cache.lock:
            if not self.closed:
                self.closed = True
                self.image = None
                self.mapping.close()
                self.entry['readers'] -= 1
                if self.entry.get('retiring') and not self.entry['readers']:
                    self.cache._remove(self.descriptor.region)


class RegisteredRGB:
    def __init__(self, native, capacity=8):
        self.native, self.capacity = native, capacity
        self.entries = OrderedDict()
        self.lock = RLock()

    def _register(self, name, size=None):
        entry = self.entries.get(name)
        if entry is None:
            # Prepared receivers remain registered even while they have no frames.
            while len(self.entries) >= self.capacity:
                idle = next((key for key, e in self.entries.items()
                             if not e['readers'] and not e['prepared']), None)
                if idle is None:
                    raise RuntimeError('registered RGB region capacity exhausted')
                self._remove(idle)
            region = SharedMemory(name=name)
            registered = False
            try:
                if size is not None and region.size != size:
                    raise ValueError('shared RGB region size mismatch')
                address = ctypes.addressof(ctypes.c_char.from_buffer(region.buf))
                self.native.check(self.native.lib.register_region(address, region.size))
                registered = True
                pointer = ctypes.c_void_p()
                self.native.check(self.native.lib.device_pointer(address, ctypes.byref(pointer)))
            except BaseException:
                if registered:
                    self.native.check(self.native.lib.unregister_region(address))
                region.close()
                raise
            entry = dict(mapping=region, address=address, pointer=pointer.value,
                         readers=0, prepared=False, retiring=False)
            self.entries[name] = entry
        elif size is not None and entry['mapping'].size != size:
            raise ValueError('shared RGB region size mismatch')
        self.entries.move_to_end(name)
        return entry

    def prepare(self, region):
        """Register an empty receiver pool without allocating/copying a frame."""
        with self.lock:
            entry = self._register(region['region'], region['size'])
            if entry['retiring']:
                raise RuntimeError('shared RGB region is retiring')
            entry['prepared'] = True

    def release(self, name):
        with self.lock:
            entry = self.entries.get(name)
            if entry is not None:
                entry['prepared'] = False
                entry['retiring'] = True
                if not entry['readers']:
                    self._remove(name)

    def acquire(self, wire):
        with self.lock:
            # Validate each frame's generation before creating a GPU reader.
            mapping = RGBMapping(wire)
            try:
                entry = self._register(wire['region'])
                if entry['retiring']:
                    raise RuntimeError('shared RGB region is retiring')
                entry['readers'] += 1
                return GPUInput(self, entry, mapping)
            except BaseException:
                mapping.close()
                raise

    def _remove(self, name):
        entry = self.entries[name]
        if entry['readers']:
            raise RuntimeError('cannot unregister active RGB readers')
        self.native.check(self.native.lib.unregister_region(entry['address']))
        entry['mapping'].close()
        del self.entries[name]

    def close(self):
        with self.lock:
            for name in list(self.entries):
                self._remove(name)
