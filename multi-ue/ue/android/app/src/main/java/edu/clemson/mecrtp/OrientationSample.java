package edu.clemson.mecrtp;

import java.io.*;
import java.util.List;

/** Bounded diagnostic capture; disk writes occur only after stream processing. */
public final class OrientationSample {
  public static final int FRAME_LIMIT = 30;
  private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
  private int frames;

  public void add(List<byte[]> nals) throws IOException {
    if (frames >= FRAME_LIMIT) return;
    if (frames == 0) {
      boolean sps = false, pps = false, idr = false;
      for (byte[] nal : nals) {
        if (nal.length == 0) throw new IOException("Empty diagnostic NAL");
        int kind = nal[0] & 31;
        sps |= kind == 7; pps |= kind == 8; idr |= kind == 5;
      }
      if (!sps || !pps || !idr) throw new IOException("Diagnostic must start with SPS/PPS and IDR");
    }
    for (byte[] nal : nals) {
      if ((long) bytes.size() + nal.length + 4 > 32 * 1024 * 1024)
        throw new IOException("Diagnostic sample exceeds 32 MiB");
      bytes.write(new byte[] {0, 0, 0, 1});
      bytes.write(nal);
    }
    frames++;
  }

  public int frames() { return frames; }

  public void save(File file) throws IOException {
    if (frames != FRAME_LIMIT) throw new IOException("Diagnostic requires 30 encoded frames");
    try (OutputStream out = new BufferedOutputStream(new FileOutputStream(file))) { bytes.writeTo(out); }
  }
}
