package edu.clemson.mecrtp;

import android.media.Image;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;

/** Shared production encoder settings and raw-image conversion for streaming and profiling. */
public final class H264Encoding {
  private H264Encoding() {}

  public static MediaFormat format(int width, int height, int fps, int bitrate) {
    MediaFormat f = MediaFormat.createVideoFormat("video/avc", width, height);
    f.setInteger(MediaFormat.KEY_COLOR_FORMAT,
        MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
    f.setInteger(MediaFormat.KEY_BIT_RATE, bitrate);
    f.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
    f.setFloat(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
    f.setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline);
    f.setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0);
    return f;
  }

  /** Android track rotation is clockwise; never infer it from UI orientation. */
  public static int rotationDegrees(MediaFormat source) {
    int value = source.containsKey(MediaFormat.KEY_ROTATION) ? source.getInteger(MediaFormat.KEY_ROTATION) : 0;
    int normalized = Math.floorMod(value, 360);
    if (normalized % 90 != 0) throw new IllegalArgumentException("Unsupported video rotation: " + value);
    return normalized;
  }

  public static void copy(Image source, Image target, int width, int height) {
    copy(source, target, width, height, 0);
  }

  public static void copy(Image source, Image target, int width, int height, int rotation) {
    if (source.getFormat() != android.graphics.ImageFormat.YUV_420_888
        || target.getFormat() != android.graphics.ImageFormat.YUV_420_888)
      throw new IllegalArgumentException("Expected flexible YUV420 images");
    android.graphics.Rect crop = source.getCropRect(), destination = target.getCropRect();
    if (crop.left < 0 || crop.top < 0 || crop.right > source.getWidth() || crop.bottom > source.getHeight()
        || crop.width() <= 0 || crop.height() <= 0
        || destination.left < 0 || destination.top < 0
        || (long) destination.left + width > target.getWidth()
        || (long) destination.top + height > target.getHeight()
        || width > destination.width() || height > destination.height()
        || width <= 0 || height <= 0
        || ((crop.left | crop.top | crop.width() | crop.height()
             | destination.left | destination.top | width | height) & 1) != 0)
      throw new IllegalArgumentException("YUV420 crop and output must be positive, even, and within images");
    for (int p = 0; p < 3; p++) {
      int scale = p == 0 ? 1 : 2;
      Image.Plane s = source.getPlanes()[p], d = target.getPlanes()[p];
      NativeYuv.plane(s.getBuffer(), s.getRowStride(), s.getPixelStride(),
          crop.left / scale, crop.top / scale, crop.width() / scale, crop.height() / scale,
          d.getBuffer(), d.getRowStride(), d.getPixelStride(),
          destination.left / scale, destination.top / scale, width / scale, height / scale, rotation);
    }
  }
}
