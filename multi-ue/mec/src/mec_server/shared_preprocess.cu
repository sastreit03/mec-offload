// Direct mapped-host RGB -> fixed NCHW FP16 model input. No RGB upload.
#include <cuda_runtime.h>
#include <cuda_fp16.h>
#include <stdint.h>
#include <math.h>
extern "C" int mapping_flags() { return cudaSetDeviceFlags(cudaDeviceMapHost); }
extern "C" const char* error_text(int code) { return cudaGetErrorString((cudaError_t)code); }
extern "C" int register_region(void* p, size_t bytes) { return cudaHostRegister(p, bytes, cudaHostRegisterMapped); }
extern "C" int device_pointer(void* p, void** out) { return cudaHostGetDevicePointer(out, p, 0); }
extern "C" int unregister_region(void* p) { return cudaHostUnregister(p); }
__device__ float half_round(float x) { return __half2float(__float2half_rn(x)); }
__device__ float pixel(const unsigned char* src, int y, int x, int c, int stride) {
    // Match the legacy .half() / 255, including its intermediate half rounding.
    return half_round((float)src[y*stride+3*x+c] * (1.f/255.f));
}
__global__ void shared_rgb_preprocess(const unsigned char* src, __half* dst,
    int w, int h, int stride, int ow, int oh, int nw, int nh, int left, int top, int normalize) {
    int i = blockIdx.x*blockDim.x+threadIdx.x;
    if (i >= 3*ow*oh) return;
    int c = i%3, x = (i/3)%ow, y = i/(3*ow);
    float value = half_round(114.f/255.f);
    if (x >= left && x < left+nw && y >= top && y < top+nh) {
        float sy = fmaxf(0.f, ((float)h/nh)*(y-top+.5f)-.5f);
        float sx = fmaxf(0.f, ((float)w/nw)*(x-left+.5f)-.5f);
        int ya = (int)sy, xa = (int)sx;
        int yb = min(ya+1,h-1), xb = min(xa+1,w-1);
        float ay = sy-ya, ax = sx-xa;
        float a = pixel(src,ya,xa,c,stride), b = pixel(src,ya,xb,c,stride);
        float d = pixel(src,yb,xa,c,stride), e = pixel(src,yb,xb,c,stride);
        value = half_round((1-ay)*((1-ax)*a+ax*b) + ay*((1-ax)*d+ax*e));
    }
    if (normalize) {
        const float mean[] = {.485f,.456f,.406f}, sd[] = {.229f,.224f,.225f};
        value = half_round(half_round(value-half_round(mean[c]))/half_round(sd[c]));
    }
    dst[c*ow*oh+y*ow+x] = __float2half_rn(value);
}
extern "C" int preprocess(uint64_t src, uint64_t dst, int w, int h, int stride,
    int ow, int oh, int nw, int nh, int left, int top, int normalize, uint64_t stream) {
    shared_rgb_preprocess<<<(3*ow*oh+255)/256,256,0,(cudaStream_t)stream>>>(
        (const unsigned char*)src, (__half*)dst,w,h,stride,ow,oh,nw,nh,left,top,normalize);
    return cudaGetLastError();
}
