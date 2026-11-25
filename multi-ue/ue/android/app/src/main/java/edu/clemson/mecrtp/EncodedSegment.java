package edu.clemson.mecrtp;

import java.io.*;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.util.Locale;
import org.json.JSONObject;

/** A short campaign segment, verified and loaded on the media thread before release. */
final class EncodedSegment {
  final byte[][] frames;
  final boolean[] idr;

  private EncodedSegment(int count) {
    frames = new byte[count][];
    idr = new boolean[count];
  }

  static String key(JSONObject assignment, int width, int height, int fps, int gop)
      throws Exception {
    return String.format(Locale.ROOT, "px1-%s-%d-%d-%dx%d-%d-%d-%d",
        assignment.getString("media_sha256").toLowerCase(Locale.ROOT),
        assignment.getInt("source_start_frame"), count(assignment, fps), width, height,
        fps, gop, assignment.getInt("bitrate_bps"));
  }

  static int count(JSONObject assignment, int fps) throws Exception {
    return Math.max(assignment.getInt("frame_count"), 8 * fps);
  }

  static EncodedSegment read(InputStream source, String key, String name, int count, int gop)
      throws Exception {
    MessageDigest hash = MessageDigest.getInstance("SHA-256");
    if (source == null) throw new IOException("Encoded asset cannot be opened");
    try (DataInputStream in = new DataInputStream(new BufferedInputStream(
        new DigestInputStream(source, hash)))) {
      if (in.readInt() != 0x50415631 || !key.equals(in.readUTF()) || in.readInt() != count)
        throw new IOException("Encoded asset header differs from the assignment");
      EncodedSegment segment = new EncodedSegment(count);
      long total = 0;
      for (int i = 0; i < count; i++) {
        int size = in.readInt(), flag = in.readUnsignedByte();
        total += size;
        if (size < 5 || total > Runtime.getRuntime().maxMemory() / 3 || flag > 1
            || (i % gop == 0 && flag != 1))
          throw new IOException("Invalid or oversized encoded segment at frame " + i);
        segment.idr[i] = flag == 1;
        segment.frames[i] = new byte[size];
        in.readFully(segment.frames[i]);
      }
      if (in.read() != -1) throw new IOException("Unexpected trailing encoded data");
      StringBuilder hex = new StringBuilder();
      for (byte b : hash.digest()) hex.append(String.format(Locale.ROOT, "%02x", b & 255));
      if (!name.equals(key + "-" + hex + ".pav")) throw new IOException("Encoded asset hash mismatch");
      return segment;
    }
  }
}
