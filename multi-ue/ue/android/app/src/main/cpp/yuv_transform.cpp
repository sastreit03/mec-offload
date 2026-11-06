#include "yuv_transform.h"
#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace mec {
namespace {
void validate(const Plane& p, int left, int top, int w, int h) {
  if (!p.data || p.rowStride <= 0 || p.pixelStride <= 0 || left < 0 || top < 0 || w <= 0 || h <= 0)
    throw std::invalid_argument("Invalid YUV plane geometry");
  uint64_t rowEnd = (uint64_t(left) + w - 1) * p.pixelStride + 1;
  uint64_t end = (uint64_t(top) + h - 1) * p.rowStride + rowEnd;
  if (rowEnd > uint64_t(p.rowStride) || end > p.size)
    throw std::invalid_argument("YUV crop/stride exceeds buffer limit");
}

// Separate rotation specializations keep branching out of the inner sample loop.
// 32x32 tiles limit the cache cost of transposing portrait input into landscape.
template<int Rotation, bool Scale>
void rotate(const Plane& s, int w, int h, const Plane& d, int dw, int dh,
            const std::vector<int>& xs, const std::vector<int>& ys) {
  constexpr int tile = 32;
  for (int by = 0; by < dh; by += tile) {
    for (int bx = 0; bx < dw; bx += tile) {
      for (int y = by; y < std::min(by + tile, dh); ++y) {
        const int ry = Scale ? ys[y] : y;
        uint8_t* out = d.data + size_t(y) * d.rowStride;
        for (int x = bx; x < std::min(bx + tile, dw); ++x) {
          const int rx = Scale ? xs[x] : x;
          int sx, sy;
          if constexpr (Rotation == 0) { sx = rx; sy = ry; }
          else if constexpr (Rotation == 90) { sx = ry; sy = h - 1 - rx; }
          else if constexpr (Rotation == 180) { sx = w - 1 - rx; sy = h - 1 - ry; }
          else { sx = w - 1 - ry; sy = rx; }
          out[size_t(x) * d.pixelStride] = s.data[size_t(sy) * s.rowStride + size_t(sx) * s.pixelStride];
        }
      }
    }
  }
}

template<bool Scale>
void dispatch(const Plane& s, int w, int h, const Plane& d, int dw, int dh, int rotation,
              const std::vector<int>& xs, const std::vector<int>& ys) {
  switch (rotation) {
    case 0: rotate<0, Scale>(s,w,h,d,dw,dh,xs,ys); break;
    case 90: rotate<90, Scale>(s,w,h,d,dw,dh,xs,ys); break;
    case 180: rotate<180, Scale>(s,w,h,d,dw,dh,xs,ys); break;
    case 270: rotate<270, Scale>(s,w,h,d,dw,dh,xs,ys); break;
  }
}
}

void transform(const Plane& source, int left, int top, int width, int height,
               const Plane& target, int targetLeft, int targetTop,
               int targetWidth, int targetHeight, int rotation) {
  if (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270)
    throw std::invalid_argument("Rotation must be 0, 90, 180, or 270");
  validate(source, left, top, width, height);
  validate(target, targetLeft, targetTop, targetWidth, targetHeight);
  Plane s = source, d = target;
  s.data += size_t(top) * s.rowStride + size_t(left) * s.pixelStride;
  d.data += size_t(targetTop) * d.rowStride + size_t(targetLeft) * d.pixelStride;
  int rotatedWidth = rotation % 180 ? height : width;
  int rotatedHeight = rotation % 180 ? width : height;
  bool scale = rotatedWidth != targetWidth || rotatedHeight != targetHeight;
  if (!scale && rotation == 0 && s.pixelStride == 1 && d.pixelStride == 1) {
    for (int y = 0; y < height; ++y)
      std::memcpy(d.data + size_t(y)*d.rowStride, s.data + size_t(y)*s.rowStride, width);
    return;
  }
  // Reuse coordinate buffers on the codec thread; no full-frame scratch allocation.
  thread_local std::vector<int> xs, ys;
  if (scale) {
    xs.resize(targetWidth); ys.resize(targetHeight);
    for (int x=0; x<targetWidth; ++x) xs[x] = int(int64_t(x)*rotatedWidth/targetWidth);
    for (int y=0; y<targetHeight; ++y) ys[y] = int(int64_t(y)*rotatedHeight/targetHeight);
    dispatch<true>(s,width,height,d,targetWidth,targetHeight,rotation,xs,ys);
  } else dispatch<false>(s,width,height,d,targetWidth,targetHeight,rotation,xs,ys);
}
}
