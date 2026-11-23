"""Standalone spawned-worker shared RGB -> CUDA preprocessing -> graph probe.

Synthetic decoded frames substitute for GStreamer. Exactly one np.copyto of
visible RGB pixels publishes each frame. No production receiver/model code is
changed. CUDA source is embedded for reproducibility; nvcc builds it in evidence.
"""
import argparse
import ctypes
import json
import multiprocessing as mp
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
import pickle
import queue
import subprocess
import time
import traceback

import numpy as np


CUDA_SOURCE = r'''
#include <cuda_runtime.h>
#include <stdint.h>
#include <math.h>
extern "C" int mapping_flags() { return cudaSetDeviceFlags(cudaDeviceMapHost); }
extern "C" const char* error_text(int code) { return cudaGetErrorString((cudaError_t)code); }
extern "C" int attribute(int which, int* value) {
    const cudaDeviceAttr attrs[] = {cudaDevAttrCanMapHostMemory,
        cudaDevAttrHostRegisterSupported, cudaDevAttrPageableMemoryAccess,
        cudaDevAttrPageableMemoryAccessUsesHostPageTables,
        cudaDevAttrCanUseHostPointerForRegisteredMem, cudaDevAttrConcurrentManagedAccess};
    return cudaDeviceGetAttribute(value, attrs[which], 0);
}
extern "C" int register_region(void* ptr, size_t bytes) {
    return cudaHostRegister(ptr, bytes, cudaHostRegisterMapped);
}
extern "C" int device_pointer(void* ptr, void** out) {
    return cudaHostGetDevicePointer(out, ptr, 0);
}
extern "C" int unregister_region(void* ptr) { return cudaHostUnregister(ptr); }
__device__ unsigned char pixel(int x, int y, int c, int fid) {
    int value = c == 0 ? x + 3*y + 17*fid : c == 1 ? 5*x + y + 29*fid : x + 7*y + 43*fid;
    return (unsigned char)(value & 255);
}
__global__ void verify_rgb(const unsigned char* src, int w, int h, int stride, int fid, int* errors) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= h*stride) return;
    int y = i/stride, p = i%stride;
    unsigned char expected = p < w*3 ? pixel(p/3, y, p%3, fid) : 0xcd;
    if (src[i] != expected) atomicAdd(errors, 1);
}
__global__ void preprocess(const unsigned char* src, float* dst, int w, int h, int stride,
                           int ow, int oh, int nw, int nh, int left, int top) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= 3*ow*oh) return;
    int c = i/(ow*oh), y = (i/ow)%oh, x = i%ow;
    float value = 114.f/255.f;
    if (x >= left && x < left+nw && y >= top && y < top+nh) {
        float sx = (x-left+.5f)*w/nw-.5f, sy = (y-top+.5f)*h/nh-.5f;
        int x0 = (int)floorf(sx), y0 = (int)floorf(sy);
        float ax = sx-x0, ay = sy-y0;
        int xa = max(0,min(w-1,x0)), xb = max(0,min(w-1,x0+1));
        int ya = max(0,min(h-1,y0)), yb = max(0,min(h-1,y0+1));
        float a = src[ya*stride+3*xa+c]/255.f, b = src[ya*stride+3*xb+c]/255.f;
        float d = src[yb*stride+3*xa+c]/255.f, e = src[yb*stride+3*xb+c]/255.f;
        value = ((1-ax)*a+ax*b)*(1-ay) + ((1-ax)*d+ax*e)*ay;
    }
    const float mean[] = {.485f,.456f,.406f}, sd[] = {.229f,.224f,.225f};
    dst[i] = (value-mean[c])/sd[c];
}
extern "C" int launch(uint64_t src, uint64_t dst, uint64_t errors, int w, int h,
                       int stride, int fid, int ow, int oh, uint64_t stream_ptr) {
    cudaStream_t stream = (cudaStream_t)stream_ptr;
    verify_rgb<<<(h*stride+255)/256,256,0,stream>>>((const unsigned char*)src,w,h,stride,fid,(int*)errors);
    cudaError_t rc = cudaGetLastError();
    if (rc != cudaSuccess) return rc;
    float scale = fminf((float)ow/w,(float)oh/h);
    int nw = (int)roundf(w*scale), nh = (int)roundf(h*scale);
    preprocess<<<(3*ow*oh+255)/256,256,0,stream>>>((const unsigned char*)src,(float*)dst,
        w,h,stride,ow,oh,nw,nh,(ow-nw)/2,(oh-nh)/2);
    return cudaGetLastError();
}
'''


ATTRIBUTES = ("can_map_host_memory", "host_register_supported", "pageable_memory_access",
              "pageable_memory_access_uses_host_page_tables", "can_use_host_pointer_for_registered_mem",
              "concurrent_managed_access")


def library(path):
    lib = ctypes.CDLL(str(path))
    lib.error_text.argtypes = [ctypes.c_int]
    lib.error_text.restype = ctypes.c_char_p
    lib.attribute.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.register_region.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.device_pointer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.unregister_region.argtypes = [ctypes.c_void_p]
    lib.launch.argtypes = [ctypes.c_uint64]*3 + [ctypes.c_int]*6 + [ctypes.c_uint64]
    return lib


def check(lib, code, operation):
    if code:
        raise RuntimeError(f"{operation}: CUDA {code}: {lib.error_text(code).decode()}")


def pattern(fid, height, width):
    # Construct synthetic source pixels; this is not a copy in the delivery path.
    y, x = np.indices((height, width), dtype=np.int32)
    return np.stack(((x+3*y+17*fid)&255, (5*x+y+29*fid)&255,
                     (x+7*y+43*fid)&255), axis=-1).astype(np.uint8)


def reference(fid, cfg):
    import torch
    import torch.nn.functional as F
    image = torch.from_numpy(pattern(fid, cfg["height"], cfg["width"]))
    x = image.permute(2, 0, 1).unsqueeze(0).float()/255
    scale = min(cfg["out_width"]/cfg["width"], cfg["out_height"]/cfg["height"])
    nw, nh = round(cfg["width"]*scale), round(cfg["height"]*scale)
    left, top = (cfg["out_width"]-nw)//2, (cfg["out_height"]-nh)//2
    x = F.interpolate(x, size=(nh, nw), mode="bilinear", align_corners=False)
    x = F.pad(x, (left, cfg["out_width"]-nw-left, top, cfg["out_height"]-nh-top), value=114/255)
    x = (x-torch.tensor([.485,.456,.406]).view(1,3,1,1))/torch.tensor([.229,.224,.225]).view(1,3,1,1)
    return (x*1.25+.125).numpy()


def worker(cfg, incoming, outgoing):
    shm = None
    registered = False
    address = None
    lib = None
    try:
        lib = library(cfg["library"])
        check(lib, lib.mapping_flags(), "cudaSetDeviceFlags(cudaDeviceMapHost)")
        import torch
        torch.set_num_threads(1)
        torch.cuda.init()
        attrs = {}
        for i, name in enumerate(ATTRIBUTES):
            value = ctypes.c_int()
            check(lib, lib.attribute(i, ctypes.byref(value)), name)
            attrs[name] = value.value
        shm = SharedMemory(name=cfg["region"])
        address = ctypes.addressof(ctypes.c_char.from_buffer(shm.buf))
        check(lib, lib.register_region(address, shm.size), "cudaHostRegisterMapped")
        registered = True
        device = ctypes.c_void_p()
        check(lib, lib.device_pointer(address, ctypes.byref(device)), "cudaHostGetDevicePointer")
        stream = torch.cuda.Stream()
        fixed = torch.zeros((1,3,cfg["out_height"],cfg["out_width"]), device="cuda")
        torch.cuda.synchronize()
        with torch.cuda.stream(stream):
            for _ in range(3):
                warmup_output = fixed*1.25+.125
        stream.synchronize()
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph, stream=stream):
            # Artificial GPU residence proves RGB release need not wait for inference.
            torch.cuda._sleep(cfg["delay_cycles"])
            graph_output = fixed*1.25+.125
        stream.synchronize()
        torch.cuda.nvtx.range_push("shared_memory_trial")
        outgoing.put(dict(type="ready", attributes=attrs, worker_host_address=address,
                          worker_device_address=device.value, mapped_bytes=shm.size,
                          mapping="cudaHostRegisterMapped + cudaHostGetDevicePointer",
                          torch_version=torch.__version__, gpu=torch.cuda.get_device_name(),
                          fixed_input_address=fixed.data_ptr(), fixed_output_address=graph_output.data_ptr()))
        pending = []
        done = False
        generations = [0]*cfg["slots"]
        count = peak = releases_while_inference_pending = 0
        max_error = 0.0
        while not done or pending:
            try:
                job = incoming.get(timeout=.0005) if not done else None
                if job is None:
                    done = True
                else:
                    slot, fid, generation = job["slot"], job["frame_id"], job["generation"]
                    assert job["region"] == cfg["region"]
                    assert 0 <= slot < cfg["slots"]
                    assert job["offset"] == slot*cfg["slot_bytes"]
                    assert job["shape"] == [cfg["height"],cfg["width"],3]
                    assert job["row_stride"] == cfg["row_stride"]
                    assert generation == generations[slot]+1
                    generations[slot] = generation
                    host_output = torch.empty_like(fixed, device="cpu", pin_memory=True)
                    host_errors = torch.empty((), dtype=torch.int32, pin_memory=True)
                    with torch.cuda.stream(stream):
                        errors = torch.zeros((), dtype=torch.int32, device="cuda")
                        check(lib, lib.launch(device.value+job["offset"], fixed.data_ptr(), errors.data_ptr(),
                                              cfg["width"], cfg["height"], cfg["row_stride"], fid,
                                              cfg["out_width"],cfg["out_height"],stream.cuda_stream), "preprocess kernels")
                        read_done = torch.cuda.Event()
                        read_done.record(stream)
                        graph.replay()
                        host_output.copy_(graph_output, non_blocking=True)
                        host_errors.copy_(errors, non_blocking=True)
                        complete = torch.cuda.Event()
                        complete.record(stream)
                    pending.append(dict(job=job, read_done=read_done, complete=complete,
                                        output=host_output, errors=host_errors, device_errors=errors,
                                        released=False))
                    peak = max(peak, len(pending))
                    assert fixed.data_ptr() == cfg.get("fixed_address", fixed.data_ptr())
                    cfg["fixed_address"] = fixed.data_ptr()
            except queue.Empty:
                pass
            remaining = []
            for entry in pending:
                job = entry["job"]
                if not entry["released"] and entry["read_done"].query():
                    still_running = not entry["complete"].query()
                    releases_while_inference_pending += still_running
                    outgoing.put(dict(type="release", slot=job["slot"], generation=job["generation"],
                                      frame_id=job["frame_id"], inference_pending=still_running))
                    entry["released"] = True
                if not entry["complete"].query():
                    remaining.append(entry)
                    continue
                mismatches = int(entry["errors"].item())
                expected = reference(job["frame_id"], cfg)
                actual = entry["output"].numpy()
                error = float(np.max(np.abs(actual-expected)))
                max_error = max(error, max_error)
                assert mismatches == 0, (job, mismatches)
                np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-5)
                count += 1
                outgoing.put(dict(type="completed", frame_id=job["frame_id"], slot=job["slot"],
                                  generation=job["generation"], rgb_mismatches=mismatches,
                                  max_absolute_output_error=error))
            pending = remaining
        torch.cuda.nvtx.range_pop()
        stream.synchronize()
        check(lib, lib.unregister_region(address), "cudaHostUnregister")
        registered = False
        outgoing.put(dict(type="drained", completed=count, peak_inflight=peak,
                          rgb_releases_before_inference_completion=releases_while_inference_pending,
                          max_absolute_output_error=max_error, generation_counts=generations,
                          mapping_unregistered=True))
    except BaseException:
        outgoing.put(dict(type="error", traceback=traceback.format_exc()))
        raise
    finally:
        if registered:
            # Teardown must never unmap memory still being read by the GPU.
            try:
                import torch
                torch.cuda.synchronize()
                check(lib, lib.unregister_region(address), "cleanup unregister")
            except Exception:
                outgoing.put(dict(type="cleanup_error", traceback=traceback.format_exc()))
        if shm is not None:
            shm.close()


def run(args):
    root = Path(args.evidence).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root/"summary.json").exists():
        raise ValueError("Use a fresh evidence directory; summary.json already exists")
    source = root/"shared_rgb_probe.cu"
    source.write_text(CUDA_SOURCE)
    so = root/"shared_rgb_probe.so"
    command = ["nvcc", "-O2", "--fmad=false", "-shared", "-Xcompiler", "-fPIC",
               "-arch=sm_121", str(source), "-o", str(so)]
    build = subprocess.run(command, capture_output=True, text=True)
    (root/"build.log").write_text(build.stdout+build.stderr)
    if build.returncode:
        raise RuntimeError(f"nvcc failed; see {root/'build.log'}")
    stride = args.width*3+128
    slot_bytes = ((stride*args.height+4095)//4096)*4096
    shm = SharedMemory(create=True, size=slot_bytes*args.slots)
    pool = np.ndarray((shm.size,), np.uint8, buffer=shm.buf)
    pool.fill(0xcd)
    cfg = dict(region=shm.name, width=args.width, height=args.height, row_stride=stride,
               out_width=640, out_height=640, slots=args.slots, slot_bytes=slot_bytes,
               delay_cycles=args.delay_cycles, library=str(so))
    ctx = mp.get_context("spawn")
    incoming, outgoing = ctx.Queue(), ctx.Queue()
    child = ctx.Process(target=worker, args=(cfg, incoming, outgoing), name="shared-rgb-cuda-probe")
    records, submitted, completed = [], [], []
    owners, generations = {}, [0]*args.slots
    free = list(range(args.slots))
    summary = dict(configuration=cfg, kind="synthetic decoded RGB shared-memory CUDA graph probe",
                   production_models_tested=False, gstreamer_mapping_tested=False,
                   preprocessing="FP32 bilinear align_corners=False letterbox + channel normalization",
                   graph="GPU delay then output = input * 1.25 + 0.125",
                   source_row_stride=args.width*3+64, parent_host_address=int(pool.ctypes.data),
                   explicit_full_frame_delivery_copies=0, submitted=0, status="failed")
    child.start()
    try:
        ready = outgoing.get(timeout=120)
        records.append(ready)
        assert ready["type"] == "ready", ready
        summary["mapping"] = ready
        sent_stop = False
        deadline = time.monotonic()+120
        while True:
            while free and len(submitted) < args.frames:
                slot = free.pop(0)
                fid = len(submitted)
                generations[slot] += 1
                # Simulate a decoded Gst frame with a different padded row stride.
                decoded_bytes = np.empty((args.height,args.width*3+64), dtype=np.uint8)
                source_view = np.ndarray((args.height,args.width,3), np.uint8,
                                         buffer=decoded_bytes, strides=(args.width*3+64,3,1))
                source_view[:] = pattern(fid,args.height,args.width)
                visible = np.ndarray(source_view.shape, np.uint8, buffer=shm.buf,
                                     offset=slot*slot_bytes, strides=(stride,3,1))
                # This is the one explicit full-frame copy in the delivery path.
                np.copyto(visible, source_view)
                summary["explicit_full_frame_delivery_copies"] += 1
                job = dict(region=shm.name, offset=slot*slot_bytes, slot=slot,
                           generation=generations[slot], frame_id=fid,
                           shape=list(source_view.shape), row_stride=stride)
                assert all(isinstance(value,(str,int,list)) for value in job.values())
                wire_bytes = len(pickle.dumps(job))
                assert wire_bytes < 512
                owners[slot] = (fid,generations[slot])
                submitted.append(dict(job, metadata_pickle_bytes=wire_bytes))
                incoming.put(job)
            if len(submitted) == args.frames and not sent_stop:
                incoming.put(None)
                sent_stop = True
            assert time.monotonic() < deadline, "worker drain timeout"
            message = outgoing.get(timeout=20)
            records.append(message)
            if message["type"] == "release":
                slot = message["slot"]
                assert owners.pop(slot) == (message["frame_id"],message["generation"])
                free.append(slot)
            elif message["type"] == "completed":
                completed.append(message["frame_id"])
            elif message["type"] == "drained":
                summary["drain"] = message
                break
            elif message["type"] in ("error","cleanup_error"):
                raise RuntimeError(message)
        child.join(15)
        assert child.exitcode == 0
        assert sorted(completed) == list(range(args.frames))
        assert not owners
        assert summary["drain"]["peak_inflight"] >= 2
        assert summary["drain"]["rgb_releases_before_inference_completion"] > 0
        assert min(generations) >= 2
        summary["status"] = "passed"
    except BaseException:
        summary["error"] = traceback.format_exc()
    finally:
        if child.is_alive():
            child.terminate()
            child.join(10)
        summary.update(worker_exitcode=child.exitcode, submitted=len(submitted),
                       unique_completed=len(set(completed)), terminal_records=len(completed),
                       slots_outstanding=len(owners),
                       metadata_max_pickle_bytes=max((j["metadata_pickle_bytes"] for j in submitted),default=0),
                       rgb_bytes_per_frame=args.width*args.height*3)
        incoming.close()
        outgoing.close()
        del pool
        # Drop views before closing the underlying mapping.
        visible = None
        shm.close()
        shm.unlink()
        summary["parent_mapping_closed_and_unlinked"] = True
        (root/"events.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records))
        (root/"submitted.jsonl").write_text("".join(json.dumps(r)+"\n" for r in submitted))
        (root/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--delay-cycles", type=int, default=30_000_000)
    args = parser.parse_args()
    if args.slots < 2 or args.frames < 2*args.slots or min(args.width,args.height,args.delay_cycles) <= 0:
        parser.error("Need at least two slots, two generations per slot, and positive dimensions/delay")
    raise SystemExit(run(args))
