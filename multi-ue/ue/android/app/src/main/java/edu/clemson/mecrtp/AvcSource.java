package edu.clemson.mecrtp;

import android.content.Context;
import android.graphics.ImageFormat;
import android.hardware.camera2.*;
import android.media.*;
import android.net.Uri;
import android.os.*;
import android.util.Range;
import java.nio.ByteBuffer;
import java.util.*;

/** Camera or paced dataset decoder -> common flexible YUV encoder -> RTP. */
public final class AvcSource {
  private final Context context;
  private final StreamSession session;
  private final StreamSession.Events events;
  private final HandlerThread thread = new HandlerThread("mec-media");
  private final Handler handler;
  private MediaCodec encoder, decoder;
  private MediaExtractor extractor;
  private CameraDevice camera;
  private CameraCaptureSession capture;
  private ImageReader reader;
  private H264Rtp rtp;
  private boolean active = true;
  private int width, height, fps, bitrate;
  private long fileStart, firstPts = -1;
  private boolean inputEos;

  public AvcSource(Context c, StreamSession s, StreamSession.Events e) throws Exception {
    context = c;
    session = s;
    events = e;
    thread.start();
    handler = new Handler(thread.getLooper());
  }

  public void start(Uri file) {
    handler.post(
        () -> {
          try {
            JSONObjectConfig();
            if (file == null) camera();
            else dataset(file);
            pump();
          } catch (Exception e) {
            fail(e);
          }
        });
  }

  private void JSONObjectConfig() throws Exception {
    org.json.JSONObject c = session.config();
    width = c.getInt("width");
    height = c.getInt("height");
    fps = c.getInt("fps");
    bitrate = c.getInt("bitrate_bps");
    MediaFormat f = MediaFormat.createVideoFormat("video/avc", width, height);
    f.setInteger(
        MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
    f.setInteger(MediaFormat.KEY_BIT_RATE, bitrate);
    f.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
    f.setFloat(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
    f.setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline);
    f.setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0);
    encoder = MediaCodec.createEncoderByType("video/avc");
    encoder.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
    encoder.start();
    rtp = new H264Rtp(session, 96, 1200, session);
  }

  private void camera() throws Exception {
    CameraManager cm = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
    String selected = null;
    for (String id : cm.getCameraIdList()) {
      Integer facing = cm.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
      if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
        selected = id;
        break;
      }
    }
    if (selected == null) throw new Exception("No rear camera");
    reader = ImageReader.newInstance(width, height, ImageFormat.YUV_420_888, 3);
    reader.setOnImageAvailableListener(
        r -> {
          Image image = r.acquireLatestImage();
          if (image != null) {
            try {
              submit(image, System.nanoTime() / 1000);
            } catch (Exception e) {
              fail(e);
            } finally {
              image.close();
            }
          }
        },
        handler);
    cm.openCamera(
        selected,
        new CameraDevice.StateCallback() {
          public void onOpened(CameraDevice d) {
            if (!active) {
              d.close();
              return;
            }
            camera = d;
            try {
              d.createCaptureSession(
                  Collections.singletonList(reader.getSurface()),
                  new CameraCaptureSession.StateCallback() {
                    public void onConfigured(CameraCaptureSession s) {
                      capture = s;
                      try {
                        CaptureRequest.Builder b =
                            d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                        b.addTarget(reader.getSurface());
                        b.set(
                            CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                            new Range<Integer>(fps, fps));
                        s.setRepeatingRequest(b.build(), null, handler);
                      } catch (Exception e) {
                        fail(e);
                      }
                    }

                    public void onConfigureFailed(CameraCaptureSession s) {
                      fail(new Exception("Camera configuration failed"));
                    }
                  },
                  handler);
            } catch (Exception e) {
              fail(e);
            }
          }

          public void onDisconnected(CameraDevice d) {
            fail(new Exception("Camera disconnected"));
          }

          public void onError(CameraDevice d, int code) {
            fail(new Exception("Camera error " + code));
          }
        },
        handler);
  }

  private void dataset(Uri uri) throws Exception {
    extractor = new MediaExtractor();
    extractor.setDataSource(context, uri, null);
    int track = -1;
    MediaFormat f = null;
    for (int i = 0; i < extractor.getTrackCount(); i++) {
      MediaFormat x = extractor.getTrackFormat(i);
      if (x.getString(MediaFormat.KEY_MIME).startsWith("video/")) {
        track = i;
        f = x;
        break;
      }
    }
    if (track < 0) throw new Exception("No video track");
    extractor.selectTrack(track);
    f.setInteger(
        MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
    decoder = MediaCodec.createDecoderByType(f.getString(MediaFormat.KEY_MIME));
    decoder.configure(f, null, null, 0);
    decoder.start();
    fileStart = session.config().getLong("boundary_unix_ns");
  }

  private void submit(Image source, long pts) throws Exception {
    if (!active) return;
    int index = encoder.dequeueInputBuffer(0);
    if (index < 0) return;
    Image target = encoder.getInputImage(index);
    if (target == null) throw new Exception("Encoder lacks flexible YUV input");
    if (!session.prepare(pts, System.nanoTime())) {
      encoder.queueInputBuffer(index, 0, 0, pts, 0);
      return;
    }
    int nextBitrate = session.config().getInt("bitrate_bps");
    Bundle b = new Bundle();
    if (nextBitrate != bitrate) {
      b.putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, nextBitrate);
      bitrate = nextBitrate;
    }
    if (session.keyframe()) b.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
    if (!b.isEmpty()) encoder.setParameters(b);
    android.graphics.Rect crop = source.getCropRect();
    for (int p = 0; p < 3; p++) {
      int scale = p == 0 ? 1 : 2;
      Image.Plane s = source.getPlanes()[p], d = target.getPlanes()[p];
      YuvScaler.plane(
          s.getBuffer(),
          s.getRowStride(),
          s.getPixelStride(),
          d.getBuffer(),
          d.getRowStride(),
          d.getPixelStride(),
          crop.left / scale,
          crop.top / scale,
          crop.width() / scale,
          crop.height() / scale,
          width / scale,
          height / scale);
    }
    ByteBuffer raw = encoder.getInputBuffer(index);
    encoder.queueInputBuffer(
        index, 0, raw == null ? width * height * 3 / 2 : raw.capacity(), pts, 0);
  }

  private void pump() {
    if (!active) return;
    try {
      if (decoder != null) {
        if (!inputEos) {
          int i = decoder.dequeueInputBuffer(0);
          if (i >= 0) {
            ByteBuffer b = decoder.getInputBuffer(i);
            int size = extractor.readSampleData(b, 0);
            if (size < 0) {
              decoder.queueInputBuffer(i, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
              inputEos = true;
            } else {
              decoder.queueInputBuffer(
                  i, 0, size, extractor.getSampleTime(), extractor.getSampleFlags());
              extractor.advance();
            }
          }
        }
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        int i = decoder.dequeueOutputBuffer(info, 0);
        if (i >= 0) {
          if (firstPts < 0) firstPts = info.presentationTimeUs;
          long release = fileStart + (info.presentationTimeUs - firstPts) * 1000;
          Image image = decoder.getOutputImage(i);
          if (image != null) {
            long wait = Math.max(0, (release - sessionClock()) / 1_000_000);
            if (wait > 0) {
              final int ix = i;
              final long pts = info.presentationTimeUs;
              handler.postDelayed(
                  () -> {
                    try {
                      Image a = decoder.getOutputImage(ix);
                      if (a != null) {
                        submit(a, pts);
                        a.close();
                      }
                      decoder.releaseOutputBuffer(ix, false);
                      pump();
                    } catch (Exception e) {
                      fail(e);
                    }
                  },
                  wait);
              drain();
              return;
            }
            submit(image, info.presentationTimeUs);
            image.close();
          }
          decoder.releaseOutputBuffer(i, false);
          if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
            drain();
            events.event(
                "media_end", new org.json.JSONObject().put("final_frame_id", session.lastFrame()));
            close();
            return;
          }
        }
      }
      drain();
      handler.postDelayed(this::pump, 2);
    } catch (Exception e) {
      fail(e);
    }
  }

  private long sessionClock() {
    return session.clockNow();
  }

  private void drain() throws Exception {
    MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
    while (active) {
      int i = encoder.dequeueOutputBuffer(info, 0);
      if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
        MediaFormat f = encoder.getOutputFormat();
        for (String key : new String[] {"csd-0", "csd-1"}) {
          ByteBuffer b = f.getByteBuffer(key);
          if (b != null) {
            byte[] a = new byte[b.remaining()];
            b.duplicate().get(a);
            rtp.configure(a);
          }
        }
        continue;
      }
      if (i < 0) break;
      ByteBuffer b = encoder.getOutputBuffer(i);
      if (b != null && info.size > 0) {
        b.position(info.offset);
        b.limit(info.offset + info.size);
        byte[] a = new byte[info.size];
        b.get(a);
        if ((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) rtp.configure(a);
        else rtp.accessUnit(a, info.presentationTimeUs);
      }
      encoder.releaseOutputBuffer(i, false);
    }
  }

  private void fail(Exception e) {
    try {
      events.event("sender_error", new org.json.JSONObject().put("reason", e.toString()));
    } catch (Exception ignored) {
    }
    close();
  }

  public void close() {
    handler.post(
        () -> {
          if (!active) return;
          active = false;
          if (capture != null) capture.close();
          if (camera != null) camera.close();
          if (reader != null) reader.close();
          for (MediaCodec c : new MediaCodec[] {encoder, decoder})
            if (c != null) {
              try {
                c.stop();
              } catch (Exception ignored) {
              }
              c.release();
            }
          if (extractor != null) extractor.release();
          thread.quitSafely();
        });
  }
}
