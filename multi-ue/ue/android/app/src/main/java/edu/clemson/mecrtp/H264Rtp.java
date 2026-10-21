package edu.clemson.mecrtp;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.*;

/** RFC6184 single NAL/FU-A and RFC8285 two-byte extension on EVERY packet. */
public final class H264Rtp {
  public interface Sink {
    void send(byte[] p) throws IOException;
  }

  public interface Metadata {
    byte[] begin(long pts, long size, boolean idr) throws IOException;
  }

  private final Sink sink;
  private final Metadata metadata;
  private final int pt,
      maxPayload,
      ssrc = new Random().nextInt(),
      timestampBase = new Random().nextInt();
  private int sequence = new Random().nextInt(65536);
  private long firstPts = -1;
  private byte[] descriptor;
  private final List<byte[]> parameters = new ArrayList<>();
  public long bytes, packets, accessUnits;

  public H264Rtp(Sink sink, int payloadType, int mtu, Metadata metadata) {
    if (mtu < 256) throw new IllegalArgumentException("MTU");
    this.sink = sink;
    pt = payloadType;
    maxPayload = mtu - 116;
    this.metadata = metadata;
  }

  public static List<byte[]> split(byte[] a) {
    List<byte[]> out = new ArrayList<>();
    int begin = -1;
    for (int i = 0; i < a.length - 2; i++) {
      int n = 0;
      if (a[i] == 0 && a[i + 1] == 0) {
        if (a[i + 2] == 1) n = 3;
        else if (i + 3 < a.length && a[i + 2] == 0 && a[i + 3] == 1) n = 4;
      }
      if (n > 0) {
        if (begin >= 0 && i > begin) out.add(Arrays.copyOfRange(a, begin, i));
        begin = i + n;
        i += n - 1;
      }
    }
    if (begin >= 0 && begin < a.length) out.add(Arrays.copyOfRange(a, begin, a.length));
    if (out.isEmpty()) {
      ByteBuffer b = ByteBuffer.wrap(a);
      while (b.remaining() >= 4) {
        int n = b.getInt();
        if (n <= 0 || n > b.remaining()) throw new IllegalArgumentException("AVCC length");
        byte[] x = new byte[n];
        b.get(x);
        out.add(x);
      }
    }
    return out;
  }

  public void configure(byte[] a) {
    for (byte[] n : split(a)) {
      int t = n[0] & 31;
      if (t == 7 || t == 8) {
        parameters.removeIf(p -> (p[0] & 31) == t);
        parameters.add(n);
      }
    }
  }

  public void accessUnit(byte[] encoded, long pts) throws IOException {
    List<byte[]> nals = split(encoded);
    boolean idr = nals.stream().anyMatch(n -> (n[0] & 31) == 5);
    configure(encoded);
    if (idr) {
      List<byte[]> all = new ArrayList<>(parameters);
      for (byte[] n : nals) if ((n[0] & 31) != 7 && (n[0] & 31) != 8) all.add(n);
      nals = all;
    }
    long size = 0;
    for (byte[] n : nals) size += n.length + 4;
    descriptor = metadata.begin(pts, size, idr);
    if (descriptor.length != 96) throw new IOException("descriptor size");
    if (firstPts < 0) firstPts = pts;
    long ts = (Integer.toUnsignedLong(timestampBase) + (pts - firstPts) * 90 / 1000) & 0xffffffffL;
    for (int i = 0; i < nals.size(); i++) nal(nals.get(i), ts, i == nals.size() - 1);
    accessUnits++;
  }

  private void nal(byte[] n, long ts, boolean marker) throws IOException {
    if (n.length <= maxPayload) {
      packet(n, ts, marker);
      return;
    }
    for (int o = 1; o < n.length; ) {
      int count = Math.min(maxPayload - 2, n.length - o);
      boolean first = o == 1, last = o + count == n.length;
      byte[] p = new byte[count + 2];
      p[0] = (byte) ((n[0] & 0xe0) | 28);
      p[1] = (byte) ((n[0] & 31) | (first ? 128 : 0) | (last ? 64 : 0));
      System.arraycopy(n, o, p, 2, count);
      packet(p, ts, marker && last);
      o += count;
    }
  }

  private void packet(byte[] payload, long ts, boolean marker) throws IOException {
    byte[] p = new byte[payload.length + 116];
    ByteBuffer b = ByteBuffer.wrap(p);
    b.put((byte) 0x90)
        .put((byte) (pt | (marker ? 128 : 0)))
        .putShort((short) sequence)
        .putInt((int) ts)
        .putInt(ssrc)
        .putShort((short) 0x1000)
        .putShort((short) 25)
        .put((byte) 1)
        .put((byte) 96)
        .put(descriptor)
        .putShort((short) 0)
        .put(payload);
    sink.send(p);
    sequence = (sequence + 1) & 65535;
    bytes += p.length;
    packets++;
  }
}
