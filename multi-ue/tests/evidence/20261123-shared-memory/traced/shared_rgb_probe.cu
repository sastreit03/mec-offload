
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
