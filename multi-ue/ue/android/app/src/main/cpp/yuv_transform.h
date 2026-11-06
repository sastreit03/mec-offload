#pragma once
#include <cstddef>
#include <cstdint>

namespace mec {
struct Plane {
  uint8_t* data;
  size_t size;
  int rowStride, pixelStride;
};
// Rotation is clockwise, matching MediaFormat.KEY_ROTATION. Crop then rotate then
// nearest-neighbor scale. No YUV->RGB conversion, frame allocation, or GPU work.
void transform(const Plane& source, int left, int top, int width, int height,
               const Plane& target, int targetLeft, int targetTop,
               int targetWidth, int targetHeight, int rotation);
}
