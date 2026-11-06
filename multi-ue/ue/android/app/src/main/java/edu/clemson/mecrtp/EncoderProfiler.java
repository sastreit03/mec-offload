package edu.clemson.mecrtp;

import android.content.Context;
import android.database.Cursor;
import android.media.*;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.DocumentsContract;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import org.json.*;

/** Offline production MediaCodec profiling: no network, frame dropping, or source-time pacing. */
public final class EncoderProfiler {
  private final Context context;
  private final AtomicBoolean cancelled;
  private final Consumer<String> progress;

  public EncoderProfiler(Context context, AtomicBoolean cancelled, Consumer<String> progress) {
    this.context = context;
    this.cancelled = cancelled;
    this.progress = progress;
  }

  private void checkCancelled() throws InterruptedException {
    if (cancelled.get()) throw new InterruptedException("Profiling cancelled");
  }

  private static final class Video {
    final String name;
    final Uri uri;
    Video(String name, Uri uri) { this.name = name; this.uri = uri; }
  }

  private List<Video> videos(Uri tree) throws Exception {
    List<Video> result = new ArrayList<>();
    Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(
        tree, DocumentsContract.getTreeDocumentId(tree));
    try (Cursor c = context.getContentResolver().query(children,
        new String[] {DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                      DocumentsContract.Document.COLUMN_DISPLAY_NAME}, null, null, null)) {
      if (c == null) throw new IOException("Cannot read selected directory");
      while (c.moveToNext()) {
        String name = c.getString(1);
        if (!name.toLowerCase(Locale.ROOT).matches(".*\\.(mp4|mov|mkv|avi)$")) continue;
        result.add(new Video(name, DocumentsContract.buildDocumentUriUsingTree(tree, c.getString(0))));
      }
    }
    result.sort(Comparator.comparing(v -> v.name));
    Set<String> names = new HashSet<>();
    for (Video v : result) if (!names.add(v.name)) throw new IOException("Duplicate filename");
    if (result.size() != 8) throw new IOException("Select exactly eight profiling videos; found " + result.size());
    return result;
  }

  private String hash(Video video) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (InputStream in = context.getContentResolver().openInputStream(video.uri)) {
      if (in == null) throw new IOException("Cannot open " + video.name);
      byte[] buffer = new byte[1024 * 1024];
      for (int n; (n = in.read(buffer)) != -1;) {
        checkCancelled();
        digest.update(buffer, 0, n);
      }
    }
    StringBuilder hex = new StringBuilder();
    for (byte b : digest.digest()) hex.append(String.format(Locale.ROOT, "%02x", b & 255));
    return hex.toString();
  }

  private static BufferedWriter writer(File file) throws IOException {
    return new BufferedWriter(new OutputStreamWriter(new FileOutputStream(file), StandardCharsets.UTF_8));
  }

  private static void metadata(File root, JSONObject metadata) throws Exception {
    try (Writer out = writer(new File(root, "profile.json"))) { out.write(metadata.toString(2)); }
  }

  public File run(Uri tree, int[] rates, int fps, int gop, int width, int height) throws Exception {
    if (rates.length == 0 || fps <= 0 || gop <= 0 || width <= 0 || height <= 0
        || (width & 1) != 0 || (height & 1) != 0) throw new IllegalArgumentException("Invalid settings");
    TreeSet<Integer> sortedRates = new TreeSet<>();
    for (int rate : rates) {
      if (rate <= 0) throw new IllegalArgumentException("Bitrates must be positive");
      sortedRates.add(rate);
    }
    List<Video> videos = videos(tree);
    File parent = context.getExternalFilesDir("profiling");
    if (parent == null) throw new IOException("External app storage unavailable");
    File root = new File(parent, "encoding-pixel-" + fps + "fps-" + gop + "gop-"
        + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(new Date())
        + "-" + UUID.randomUUID().toString().substring(0, 8));
    if (!root.mkdirs()) throw new IOException("Cannot create " + root);
    JSONObject meta = new JSONObject()
        .put("encoder", "android_mediacodec_offline_v1")
        .put("status", "running").put("fps", fps).put("gop_frames", gop)
        .put("width", width).put("height", height)
        .put("bitrates_bps", new JSONArray(sortedRates))
        .put("manufacturer", Build.MANUFACTURER).put("device_model", Build.MODEL)
        .put("android_release", Build.VERSION.RELEASE).put("android_sdk", Build.VERSION.SDK_INT)
        .put("build_fingerprint", Build.FINGERPRINT)
        .put("timestamp_policy", "retain_all_frames_retime_frame_index_over_fps")
        .put("image_conversion", "native_cpu_rotate_scale_v1")
        .put("byte_accounting", "production_canonical_annexb_with_sps_pps_on_idr")
        .put("bitrate_mode", "CBR")
        .put("bitrate_mode_requested", MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
        .put("videos", new JSONArray()).put("source_sha256", new JSONObject());
    for (Video v : videos) meta.getJSONArray("videos").put(v.name);
    metadata(root, meta);
    progress.accept("Output: " + root + "\nHashing eight source videos…");
    try {
      for (Video v : videos) meta.getJSONObject("source_sha256").put(v.name, hash(v));
      metadata(root, meta);
      try (BufferedWriter csv = writer(new File(root, "frame-sizes.csv"));
           BufferedWriter streams = writer(new File(root, "streams.jsonl"))) {
        csv.write("video,bitrate_bps,frame_id,gop_id,gop_frame,frame_type,encoded_bytes,pts_us,source_pts_us,encode_elapsed_ns,codec_output_bytes\n");
        int done = 0;
        for (int rate : sortedRates) for (Video v : videos) {
          checkCancelled();
          progress.accept("Encoding " + (++done) + "/" + (videos.size() * sortedRates.size())
              + ": " + v.name + " at " + rate + " bit/s");
          JSONObject result = encode(v, rate, fps, gop, width, height, csv);
          streams.write(result.toString());
          streams.newLine();
          streams.flush();
          csv.flush();
        }
      }
      meta.put("status", "complete");
      metadata(root, meta);
      return root;
    } catch (Exception e) {
      meta.put("status", cancelled.get() ? "cancelled" : "failed").put("error", e.toString());
      metadata(root, meta);
      throw e;
    }
  }

  private JSONObject encode(Video video, int rate, int fps, int gop, int width, int height,
                            BufferedWriter csv) throws Exception {
    MediaExtractor extractor = new MediaExtractor();
    MediaCodec decoder = null, encoder = null;
    boolean decoderStarted = false, encoderStarted = false;
    long started = System.nanoTime(), lastProgress = started;
    int submitted = 0, produced = 0, pending = -1;
    long pendingSourcePts = 0, sourcePts = 0;
    boolean inputEos = false, decoderEos = false, encoderEosSent = false, outputEos = false;
    H264Rtp payload = new H264Rtp(p -> {}, 96, 1200, (pts, size, idr) -> new byte[96]);
    MediaCodec.BufferInfo decInfo = new MediaCodec.BufferInfo(), encInfo = new MediaCodec.BufferInfo();
    String outputFormat = "";
    try {
      extractor.setDataSource(context, video.uri, null);
      MediaFormat source = null;
      for (int i = 0; i < extractor.getTrackCount(); i++) {
        MediaFormat f = extractor.getTrackFormat(i);
        if (f.getString(MediaFormat.KEY_MIME).startsWith("video/")) {
          extractor.selectTrack(i); source = f; break;
        }
      }
      if (source == null) throw new IOException("No video track: " + video.name);
      String sourceFormat = source.toString();
      int sourceRotation = H264Encoding.rotationDegrees(source);
      // Native conversion applies the track rotation exactly once.
      source.setInteger(MediaFormat.KEY_ROTATION, 0);
      source.setInteger(MediaFormat.KEY_COLOR_FORMAT,
          MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
      decoder = MediaCodec.createDecoderByType(source.getString(MediaFormat.KEY_MIME));
      decoder.configure(source, null, null, 0);
      decoder.start(); decoderStarted = true;
      MediaFormat requested = H264Encoding.format(width, height, fps, rate);
      encoder = MediaCodec.createEncoderByType("video/avc");
      int mode = MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR;
      boolean supported = encoder.getCodecInfo()
          .getCapabilitiesForType("video/avc")
          .getEncoderCapabilities()
          .isBitrateModeSupported(mode);
      if (!supported) throw new IOException("Selected encoder does not support CBR");
      requested.setInteger(MediaFormat.KEY_BITRATE_MODE, mode);
      encoder.configure(requested, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
      MediaFormat acceptedInput = encoder.getInputFormat();
      android.util.Log.i("EncoderProfiler", "Accepted input format: " + acceptedInput);
      encoder.start(); encoderStarted = true;
      while (!outputEos) {
        checkCancelled();
        boolean progressMade = false;
        // Drain output before requesting more input to avoid codec backpressure deadlocks.
        int out = encoder.dequeueOutputBuffer(encInfo, 0);
        if (out == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
          MediaFormat f = encoder.getOutputFormat();
          outputFormat = f.toString();
          for (String key : new String[] {"csd-0", "csd-1"}) {
            ByteBuffer data = f.getByteBuffer(key);
            if (data != null) {
              byte[] bytes = new byte[data.remaining()]; data.duplicate().get(bytes);
              payload.configure(bytes);
            }
          }
          progressMade = true;
        } else if (out >= 0) {
          try {
            if (encInfo.size > 0) {
              ByteBuffer b = encoder.getOutputBuffer(out);
              b.position(encInfo.offset); b.limit(encInfo.offset + encInfo.size);
              byte[] data = new byte[encInfo.size]; b.get(data);
              if ((encInfo.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) payload.configure(data);
              else {
                if (produced >= submitted || encInfo.presentationTimeUs != Math.round(produced * 1_000_000.0 / fps))
                  throw new IOException("Encoder output frame/timestamp mismatch");
                List<byte[]> nals = payload.accessUnitNals(data);
                if (nals.isEmpty()) throw new IOException("Empty encoded access unit");
                boolean idr = nals.stream().anyMatch(n -> (n[0] & 31) == 5);
                if (produced % gop == 0 && !idr) throw new IOException("Missing requested IDR at " + produced);
                long bytes = 0;
                for (byte[] nal : nals) bytes += nal.length + 4;
                csv.write("\"" + video.name.replace("\"", "\"\"") + "\"," + rate + "," + produced + ","
                    + produced / gop + "," + produced % gop + "," + (idr ? "I" : "P") + "," + bytes
                    + "," + encInfo.presentationTimeUs + "," + sourcePts + "," + (System.nanoTime() - started)
                    + "," + encInfo.size + "\n");
                produced++;
              }
            }
            outputEos = (encInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
          } finally { encoder.releaseOutputBuffer(out, false); }
          progressMade = true;
        }
        if (!inputEos) {
          int in = decoder.dequeueInputBuffer(0);
          if (in >= 0) {
            ByteBuffer b = decoder.getInputBuffer(in);
            int size = extractor.readSampleData(b, 0);
            if (size < 0) {
              decoder.queueInputBuffer(in, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
              inputEos = true;
            } else {
              decoder.queueInputBuffer(in, 0, size, extractor.getSampleTime(), extractor.getSampleFlags());
              extractor.advance();
            }
            progressMade = true;
          }
        }
        if (pending < 0 && !decoderEos) {
          int index = decoder.dequeueOutputBuffer(decInfo, 0);
          if (index >= 0) {
            decoderEos = (decInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
            if (decInfo.size > 0) { pending = index; pendingSourcePts = decInfo.presentationTimeUs; }
            else decoder.releaseOutputBuffer(index, false);
            progressMade = true;
          } else if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) progressMade = true;
        }
        // Keep one input frame outstanding, matching the production session's input fence.
        if (pending >= 0 && submitted == produced) {
          int in = encoder.dequeueInputBuffer(0);
          if (in >= 0) {
            try (Image raw = decoder.getOutputImage(pending); Image target = encoder.getInputImage(in)) {
              if (raw == null || target == null) throw new IOException("Codec lacks flexible YUV images");
              H264Encoding.copy(raw, target, width, height, sourceRotation);
              if (submitted % gop == 0) {
                Bundle params = new Bundle();
                params.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
                encoder.setParameters(params);
              }
              ByteBuffer buffer = encoder.getInputBuffer(in);
              sourcePts = pendingSourcePts;
              encoder.queueInputBuffer(in, 0, buffer == null ? width * height * 3 / 2 : buffer.capacity(),
                  Math.round(submitted * 1_000_000.0 / fps), 0);
              submitted++;
            }
            decoder.releaseOutputBuffer(pending, false); pending = -1;
            progressMade = true;
          }
        }
        if (decoderEos && pending < 0 && !encoderEosSent) {
          int in = encoder.dequeueInputBuffer(0);
          if (in >= 0) {
            encoder.queueInputBuffer(in, 0, 0, Math.round(submitted * 1_000_000.0 / fps),
                MediaCodec.BUFFER_FLAG_END_OF_STREAM);
            encoderEosSent = true; progressMade = true;
          }
        }
        if (progressMade) lastProgress = System.nanoTime();
        else {
          if (System.nanoTime() - lastProgress > 30_000_000_000L) throw new IOException("Codec stalled for 30 seconds");
          Thread.sleep(1);
        }
      }
      if (!encoderEosSent || submitted == 0 || submitted != produced)
        throw new IOException("Incomplete drain: submitted=" + submitted + " produced=" + produced);
      return new JSONObject().put("video", video.name).put("bitrate_bps", rate)
          .put("submitted_frames", submitted).put("encoded_frames", produced).put("encoder_eos", true)
          .put("encoder_name", encoder.getName()).put("decoder_name", decoder.getName())
          .put("requested_format", requested.toString()).put("output_format", outputFormat)
          .put("source_rotation_degrees", sourceRotation)
          .put("image_conversion", "native_cpu_rotate_scale_v1").put("source_format", sourceFormat).put("elapsed_ns", System.nanoTime() - started);
    } finally {
      try {
        if (encoder != null) { try { if (encoderStarted) encoder.stop(); } finally { encoder.release(); } }
      } finally {
        try {
          if (decoder != null) { try { if (decoderStarted) decoder.stop(); } finally { decoder.release(); } }
        } finally { extractor.release(); }
      }
    }
  }
}
