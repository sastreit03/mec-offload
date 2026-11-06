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

/** Paced CBR loopback submission using production conversion and RTP construct/send order. */
public final class LocalSubmissionProfiler {
  private final Context context;
  private final AtomicBoolean cancelled;
  private final Consumer<String> progress;

  public LocalSubmissionProfiler(Context context, AtomicBoolean cancelled, Consumer<String> progress) {
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
    timingHeaderWritten = false;
    TreeSet<Integer> sortedRates = new TreeSet<>();
    for (int rate : rates) {
      if (rate <= 0) throw new IllegalArgumentException("Bitrates must be positive");
      sortedRates.add(rate);
    }
    List<Video> videos = videos(tree);
    File parent = context.getExternalFilesDir("profiling");
    if (parent == null) throw new IOException("External app storage unavailable");
    File root = new File(parent, "video-submission-pixel-" + fps + "fps-" + gop + "gop-"
        + new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(new Date())
        + "-" + UUID.randomUUID().toString().substring(0, 8));
    if (!root.mkdirs()) throw new IOException("Cannot create " + root);
    JSONObject meta = new JSONObject()
        .put("encoder", "android_local_submission_v1")
        .put("destination", "127.0.0.1").put("mtu", 1200)
        .put("schedule", "absolute frame-index/FPS releases; retain all frames")
        .put("deadline", "last socket send return before next scheduled release")
        .put("decoder_latency", "source PTS matched; includes buffering and polling; separate from sender processing")
        .put("warmup_frames_excluded", 0).put("status", "running").put("fps", fps).put("gop_frames", gop)
        .put("width", width).put("height", height)
        .put("bitrates_bps", new JSONArray(sortedRates))
        .put("manufacturer", Build.MANUFACTURER).put("device_model", Build.MODEL)
        .put("android_release", Build.VERSION.RELEASE).put("android_sdk", Build.VERSION.SDK_INT)
        .put("build_fingerprint", Build.FINGERPRINT)
        .put("timestamp_policy", "retain_all_frames_retime_frame_index_over_fps")
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
      try (BufferedWriter csv = writer(new File(root, "frame-timings.csv"));
           BufferedWriter streams = writer(new File(root, "streams.jsonl"));
           BufferedWriter summaries = writer(new File(root, "summary.csv"))) {
        boolean allDeadlines = true, allReceipts = true;
        Map<String, Integer> counts = new HashMap<>();
        int done = 0;
        for (int rate : sortedRates) for (Video v : videos) {
          checkCancelled();
          progress.accept("Encoding " + (++done) + "/" + (videos.size() * sortedRates.size())
              + ": " + v.name + " at " + rate + " bit/s");
          JSONObject result = encode(v, rate, fps, gop, width, height, csv);
          int count = result.getInt("encoded_frames");
          Integer previous = counts.put(v.name, count);
          if (previous != null && previous != count) throw new IOException("Frame count changed across bitrates");
          writeRow(summaries, summary, done == 1);
          summaries.flush();
          allDeadlines &= (boolean) summary.get("all_frames_submitted_by_deadline");
          allReceipts &= (boolean) summary.get("all_receipts_verified");
          streams.write(result.toString());
          streams.newLine();
          streams.flush();
          csv.flush();
        }
        meta.put("all_streams_meet_frame_deadlines", allDeadlines)
            .put("all_receipts_verified", allReceipts);
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

  private Map<String, Object> summary;
  private boolean timingHeaderWritten;

  private static void writeRow(BufferedWriter out, Map<String, Object> row, boolean header) throws IOException {
    if (header) { out.write(String.join(",", row.keySet())); out.newLine(); }
    List<String> values = new ArrayList<>();
    for (Object value : row.values()) values.add("\"" + String.valueOf(value).replace("\"", "\"\"") + "\"");
    out.write(String.join(",", values)); out.newLine();
  }

  private static void put(Map<String, Object> row, Object... pairs) {
    for (int i = 0; i < pairs.length; i += 2) row.put((String) pairs[i], pairs[i + 1]);
  }

  private static double percentile(List<Double> values, double p) {
    Collections.sort(values);
    double index = (values.size() - 1) * p;
    int low = (int) index, high = Math.min(low + 1, values.size() - 1);
    return values.get(low) + (values.get(high) - values.get(low)) * (index - low);
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
    ProfilingLoopback loopback = new ProfilingLoopback();
    UUID videoId = UUID.randomUUID();
    long[] active = new long[5]; // frame ID, raw-image preparation start, descriptor timestamp
    H264Rtp payload = new H264Rtp(loopback::send, 96, 1200, (pts, size, idr) -> {
      if (active[0] % gop == 0 && !idr) throw new IOException("Missing requested IDR");
      active[3] = size; active[4] = idr ? 1 : 0;
      return FrameDescriptor.encode(videoId, 1, active[0], active[0] / gop, 1, 1,
          idr ? 1 : 3, size, active[1], active[2], 0, 0, 3);
    });
    List<Map<String, Object>> rows = new ArrayList<>();
    List<byte[]> hashes = new ArrayList<>();
    Map<Long, ArrayDeque<Long>> decoderQueued = new HashMap<>();
    long pendingQueued = 0, pendingObserved = 0, origin = 0;
    Map<String, Object> current = null;
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
      android.util.Log.i("LocalSubmissionProfiler", "Accepted input format: " + acceptedInput);
      encoder.start(); encoderStarted = true;
      origin = System.nanoTime();
      while (!outputEos) {
        checkCancelled();
        boolean progressMade = false;
        // Drain output before requesting more input to avoid codec backpressure deadlocks.
        int out = encoder.dequeueOutputBuffer(encInfo, 0);
        long outputObserved = System.nanoTime();
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
                if (produced >= submitted || encInfo.presentationTimeUs != ProfilingTimeline.ptsUs(produced, fps))
                  throw new IOException("Encoder output frame/timestamp mismatch");
                if (current == null) throw new IOException("Missing timing for encoder output");
                if ((encInfo.flags & MediaCodec.BUFFER_FLAG_PARTIAL_FRAME) != 0)
                  throw new IOException("Partial encoder output unsupported");
                long outputCopied = System.nanoTime();
                loopback.beginFrame();
                active[0] = produced;
                active[1] = origin + (long) current.get("prepare_start_ns");
                active[2] = System.nanoTime();
                long rtpStart = System.nanoTime();
                payload.accessUnit(data, encInfo.presentationTimeUs);
                long rtpEnd = System.nanoTime();
                long lastSend = loopback.lastSendNs;
                if (loopback.packets == 0) throw new IOException("Empty RTP access unit");
                put(current, "encoded_bytes", active[3], "frame_type", active[4] == 1 ? "I" : "P",
                    "output_observed_ns", outputObserved - origin,
                    "encoder_latency_ns", outputObserved - origin - (long) current.get("encoder_queue_end_ns"),
                    "output_copy_ns", outputCopied - outputObserved,
                    "rtp_total_ns", rtpEnd - rtpStart,
                    "socket_submission_ns", loopback.socketNs,
                    "verification_bookkeeping_ns", loopback.verificationNs,
                    "rtp_non_socket_ns", rtpEnd - rtpStart - loopback.socketNs - loopback.verificationNs,
                    "first_send_ns", loopback.firstSendNs - origin, "last_send_ns", lastSend - origin,
                    "processing_ns", lastSend - active[1],
                    "release_to_submission_ns", lastSend - origin - (long) current.get("scheduled_release_ns"),
                    "deadline_lateness_ns", Math.max(0, lastSend - origin - (long) current.get("deadline_ns")),
                    "deadline_missed", lastSend - origin > (long) current.get("deadline_ns"),
                    "submitted_packets", loopback.packets, "submitted_udp_bytes", loopback.bytes,
                    "codec_output_bytes", encInfo.size);
                rows.add(current); hashes.add(loopback.sentHash()); current = null;
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
              long sourceTime = extractor.getSampleTime();
              decoder.queueInputBuffer(in, 0, size, sourceTime, extractor.getSampleFlags());
              decoderQueued.computeIfAbsent(sourceTime, ignored -> new ArrayDeque<>()).add(System.nanoTime());
              extractor.advance();
            }
            progressMade = true;
          }
        }
        if (pending < 0 && !decoderEos) {
          int index = decoder.dequeueOutputBuffer(decInfo, 0);
          if (index >= 0) {
            decoderEos = (decInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
            if (decInfo.size > 0) {
              pending = index; pendingSourcePts = decInfo.presentationTimeUs;
              pendingObserved = System.nanoTime();
              ArrayDeque<Long> times = decoderQueued.get(pendingSourcePts);
              if (times == null || times.isEmpty()) throw new IOException("Unmatched decoder source PTS");
              pendingQueued = times.remove();
              if (times.isEmpty()) decoderQueued.remove(pendingSourcePts);
            }
            else decoder.releaseOutputBuffer(index, false);
            progressMade = true;
          } else if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) progressMade = true;
        }
        // Keep one input frame outstanding, matching the production session's input fence.
        if (pending >= 0 && submitted == produced
            && System.nanoTime() >= origin + ProfilingTimeline.releaseNs(submitted, fps)) {
          int in = encoder.dequeueInputBuffer(0);
          if (in >= 0) {
            try (Image raw = decoder.getOutputImage(pending); Image target = encoder.getInputImage(in)) {
              if (raw == null || target == null) throw new IOException("Codec lacks flexible YUV images");
              long prepareStart = System.nanoTime();
              H264Encoding.copy(raw, target, width, height);
              long copyEnd = System.nanoTime();
              if (submitted % gop == 0) {
                Bundle params = new Bundle();
                params.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
                encoder.setParameters(params);
              }
              ByteBuffer buffer = encoder.getInputBuffer(in);
              sourcePts = pendingSourcePts;
              long queueStart = System.nanoTime();
              encoder.queueInputBuffer(in, 0, buffer == null ? width * height * 3 / 2 : buffer.capacity(),
                  ProfilingTimeline.ptsUs(submitted, fps), 0);
              long queueEnd = System.nanoTime();
              current = new LinkedHashMap<>();
              long release = ProfilingTimeline.releaseNs(submitted, fps);
              put(current, "video", video.name, "bitrate_bps", rate, "frame_id", submitted,
                  "pts_us", ProfilingTimeline.ptsUs(submitted, fps), "source_pts_us", sourcePts,
                  "scheduled_release_ns", release,
                  "deadline_ns", ProfilingTimeline.releaseNs(submitted + 1, fps),
                  "decoder_queue_ns", pendingQueued - origin, "decoder_observed_ns", pendingObserved - origin,
                  "decoder_latency_ns", pendingObserved - pendingQueued,
                  "decoded_hold_ns", prepareStart - pendingObserved,
                  "prepare_start_ns", prepareStart - origin, "copy_ns", copyEnd - prepareStart,
                  "encoder_queue_start_ns", queueStart - origin, "encoder_queue_end_ns", queueEnd - origin,
                  "encoder_queue_call_ns", queueEnd - queueStart,
                  "start_lateness_ns", Math.max(0, prepareStart - origin - release));
              submitted++;
            }
            decoder.releaseOutputBuffer(pending, false); pending = -1;
            progressMade = true;
          }
        }
        if (decoderEos && pending < 0 && !encoderEosSent) {
          int in = encoder.dequeueInputBuffer(0);
          if (in >= 0) {
            encoder.queueInputBuffer(in, 0, 0, ProfilingTimeline.ptsUs(submitted, fps),
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
      Map<Long, ProfilingLoopback.Receipt> receipts = loopback.finish(payload.packets);
      int misses = 0, verified = 0;
      long receivedPackets = 0;
      for (int i = 0; i < rows.size(); i++) {
        Map<String, Object> row = rows.get(i);
        ProfilingLoopback.Receipt receipt = receipts.get((long) i);
        boolean valid = ProfilingLoopback.verified(receipt, (long) row.get("submitted_packets"),
            (long) row.get("submitted_udp_bytes"), hashes.get(i));
        if (valid) verified++;
        if ((boolean) row.get("deadline_missed")) misses++;
        receivedPackets += receipt == null ? 0 : receipt.packets;
        put(row, "received_packets", receipt == null ? 0 : receipt.packets,
            "received_udp_bytes", receipt == null ? 0 : receipt.bytes,
            "receiver_first_ns", receipt == null ? "" : receipt.firstNs - origin,
            "receiver_last_ns", receipt == null ? "" : receipt.lastNs - origin,
            "receipt_verified", valid);
        writeRow(csv, row, !timingHeaderWritten); timingHeaderWritten = true;
      }
      summary = new LinkedHashMap<>();
      put(summary, "video", video.name, "bitrate_bps", rate, "frames", produced,
          "fps", fps, "frame_budget_ms", 1000.0 / fps, "setup_ms", (origin - started) / 1e6,
          "deadline_misses", misses, "verified_frames", verified,
          "submitted_packets", payload.packets, "received_packets", receivedPackets,
          "all_frames_submitted_by_deadline", misses == 0, "all_receipts_verified", verified == produced,
          "final_deadline_lateness_ms", (long) rows.get(rows.size()-1).get("deadline_lateness_ns") / 1e6);
      for (String field : new String[] {"decoder_latency", "decoded_hold", "copy", "encoder_queue_call",
          "encoder_latency", "output_copy", "rtp_total", "socket_submission", "verification_bookkeeping",
          "rtp_non_socket", "processing", "start_lateness", "release_to_submission", "deadline_lateness"}) {
        List<Double> values = new ArrayList<>();
        for (Map<String, Object> row : rows) values.add(((Number) row.get(field + "_ns")).doubleValue() / 1e6);
        put(summary, field + "_mean_ms", values.stream().mapToDouble(Double::doubleValue).average().getAsDouble(),
            field + "_p95_ms", percentile(values, .95), field + "_p99_ms", percentile(values, .99),
            field + "_max_ms", Collections.max(values));
      }
      return new JSONObject().put("video", video.name).put("bitrate_bps", rate)
          .put("submitted_frames", submitted).put("encoded_frames", produced).put("encoder_eos", true)
          .put("encoder_name", encoder.getName()).put("decoder_name", decoder.getName())
          .put("requested_format", requested.toString()).put("accepted_input_format", acceptedInput.toString())
          .put("receive_buffer_bytes", loopback.receiveBufferBytes()).put("send_buffer_bytes", loopback.sendBufferBytes())
          .put("output_format", outputFormat)
          .put("source_format", sourceFormat).put("elapsed_ns", System.nanoTime() - started);
    } finally {
      loopback.close();
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
