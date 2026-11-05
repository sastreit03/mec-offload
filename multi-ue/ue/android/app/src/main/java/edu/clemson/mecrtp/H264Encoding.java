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

  public static void copy(Image source, Image target, int width, int height) {
    android.graphics.Rect crop = source.getCropRect();
    for (int p = 0; p < 3; p++) {
      int scale = p == 0 ? 1 : 2;
      Image.Plane s = source.getPlanes()[p], d = target.getPlanes()[p];
      YuvScaler.plane(s.getBuffer(), s.getRowStride(), s.getPixelStride(),
          d.getBuffer(), d.getRowStride(), d.getPixelStride(),
          crop.left / scale, crop.top / scale, crop.width() / scale, crop.height() / scale,
          width / scale, height / scale);
    }
  }
}
