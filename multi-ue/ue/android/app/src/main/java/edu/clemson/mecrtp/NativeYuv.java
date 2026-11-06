package edu.clemson.mecrtp;

import java.nio.ByteBuffer;

/** CPU-only direct-buffer conversion: padded planar/interleaved YUV, rotation, optional scaling. */
public final class NativeYuv {
  static { System.loadLibrary("mec_yuv"); }
  private NativeYuv() {}

  public static void plane(ByteBuffer source, int sourceRow, int sourcePixel,
      int left, int top, int width, int height, ByteBuffer target, int targetRow, int targetPixel,
      int targetLeft, int targetTop, int targetWidth, int targetHeight, int rotation) {
    if (!source.isDirect() || !target.isDirect() || target.isReadOnly())
      throw new IllegalArgumentException("Native YUV requires direct input and writable direct output");
    transformPlane(source, source.position(), source.limit(), sourceRow, sourcePixel,
        left, top, width, height, target, target.position(), target.limit(), targetRow, targetPixel,
        targetLeft, targetTop, targetWidth, targetHeight, rotation);
  }

  private static native void transformPlane(ByteBuffer source, int sourcePosition, int sourceLimit,
      int sourceRow, int sourcePixel, int left, int top, int width, int height,
      ByteBuffer target, int targetPosition, int targetLimit, int targetRow, int targetPixel,
      int targetLeft, int targetTop, int targetWidth, int targetHeight, int rotation);
}
