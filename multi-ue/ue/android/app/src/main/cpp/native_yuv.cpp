#include "yuv_transform.h"
#include <jni.h>
#include <exception>
#include <stdexcept>

namespace {
mec::Plane plane(JNIEnv* env, jobject buffer, jint position, jint limit, jint row, jint pixel) {
  auto* data = static_cast<uint8_t*>(env->GetDirectBufferAddress(buffer));
  jlong capacity = env->GetDirectBufferCapacity(buffer);
  if (!data || position < 0 || limit < position || capacity < limit)
    throw std::invalid_argument("YUV conversion requires valid direct buffers");
  return {data + position, size_t(limit - position), row, pixel};
}
}
extern "C" JNIEXPORT void JNICALL
Java_edu_clemson_mecrtp_NativeYuv_transformPlane(
    JNIEnv* env, jclass, jobject source, jint sourcePosition, jint sourceLimit,
    jint sourceRow, jint sourcePixel, jint left, jint top, jint width, jint height,
    jobject target, jint targetPosition, jint targetLimit, jint targetRow, jint targetPixel,
    jint targetLeft, jint targetTop, jint targetWidth, jint targetHeight, jint rotation) {
  try {
    mec::transform(plane(env, source, sourcePosition, sourceLimit, sourceRow, sourcePixel),
                   left, top, width, height,
                   plane(env, target, targetPosition, targetLimit, targetRow, targetPixel),
                   targetLeft, targetTop, targetWidth, targetHeight, rotation);
  } catch (const std::invalid_argument& e) {
    env->ThrowNew(env->FindClass("java/lang/IllegalArgumentException"), e.what());
  } catch (const std::exception& e) {
    env->ThrowNew(env->FindClass("java/lang/IllegalStateException"), e.what());
  }
}
