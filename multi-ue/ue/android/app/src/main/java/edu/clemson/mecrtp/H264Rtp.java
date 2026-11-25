package edu.clemson.mecrtp;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.*;

/** RFC6184 single NAL/FU-A and RFC8285 two-byte extension on EVERY packet. */
public final class H264Rtp {
  public interface Sink {
    // Consume synchronously; the buffer is reused immediately after return.
    void send(byte[] p, int length) throws IOException;
  }

  public interface Metadata {
    byte[] begin(long pts, long size, boolean idr) throws IOException;
    default void complete() throws IOException {}
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
  private final byte[] packetBuffer;
  private final ByteBuffer packetWriter;
  private final List<ByteBuffer> parameters = new ArrayList<>();
  public long bytes, packets, accessUnits;

  public H264Rtp(Sink sink, int payloadType, int mtu, Metadata metadata) {
    if (mtu < 256) throw new IllegalArgumentException("MTU");
    this.sink = sink;
    pt = payloadType;
    maxPayload = mtu - 116;
    this.metadata = metadata;
    packetBuffer = new byte[mtu];
    packetWriter = ByteBuffer.wrap(packetBuffer);
  }

  private static ByteBuffer range(ByteBuffer source, int start, int end) {
    ByteBuffer view = source.duplicate();
    view.position(start);
    view.limit(end);
    return view.slice();
  }

  private static List<ByteBuffer> splitViews(ByteBuffer encoded) {
    ByteBuffer a = encoded.slice();
    List<ByteBuffer> out = new ArrayList<>();
    int length = a.remaining(), begin = -1;
    for (int i = 0; i + 2 < length; i++) {
      if (a.get(i) != 0 || a.get(i + 1) != 0) continue;
      int prefix;
      if (a.get(i + 2) == 1) prefix = 3;
      else if (i + 3 < length && a.get(i + 2) == 0 && a.get(i + 3) == 1) prefix = 4;
      else continue;
      if (begin >= 0 && i > begin) out.add(range(a, begin, i));
      begin = i + prefix;
      i = begin - 1;
    }
    if (begin >= 0 && begin < length) out.add(range(a, begin, length));
    if (out.isEmpty()) {
      while (a.remaining() >= 4) {
        int n = a.getInt();
        if (n <= 0 || n > a.remaining()) throw new IllegalArgumentException("AVCC length");
        out.add(range(a, a.position(), a.position() + n));
        a.position(a.position() + n);
      }
    }
    return out;
  }

  private static byte[] copy(ByteBuffer view) {
    byte[] bytes = new byte[view.remaining()];
    view.duplicate().get(bytes);
    return bytes;
  }

  private static List<byte[]> copies(List<ByteBuffer> views) {
    List<byte[]> out = new ArrayList<>(views.size());
    for (ByteBuffer view : views) out.add(copy(view));
    return out;
  }

  public static List<byte[]> split(byte[] a) {
    return copies(splitViews(ByteBuffer.wrap(a)));
  }

  public void configure(byte[] a) { configure(ByteBuffer.wrap(a)); }

  public void configure(ByteBuffer a) { cacheParameters(splitViews(a)); }

  private void cacheParameters(List<ByteBuffer> nals) {
    for (ByteBuffer n : nals) {
      int type = n.get(0) & 31;
      if (type == 7 || type == 8) {
        parameters.removeIf(p -> (p.get(0) & 31) == type);
        parameters.add(ByteBuffer.wrap(copy(n)));
      }
    }
  }

  private List<ByteBuffer> payloadViews(ByteBuffer encoded) {
    List<ByteBuffer> nals = splitViews(encoded);
    cacheParameters(nals);
    if (nals.stream().anyMatch(n -> (n.get(0) & 31) == 5)) {
      List<ByteBuffer> all = new ArrayList<>(parameters);
      for (ByteBuffer n : nals) {
        int type = n.get(0) & 31;
        if (type != 7 && type != 8) all.add(n);
      }
      return all;
    }
    return nals;
  }

  public List<byte[]> accessUnitNals(byte[] encoded) {
    return copies(payloadViews(ByteBuffer.wrap(encoded)));
  }

  public void accessUnit(byte[] encoded, long pts) throws IOException {
    accessUnit(ByteBuffer.wrap(encoded), pts);
  }

  public void accessUnit(ByteBuffer encoded, long pts) throws IOException {
    List<ByteBuffer> nals = payloadViews(encoded);
    boolean idr = nals.stream().anyMatch(n -> (n.get(0) & 31) == 5);
    long size = 0;
    for (ByteBuffer n : nals) size += n.remaining() + 4;
    // Canonical size includes four-byte Annex-B prefixes, but excludes transport headers.
    descriptor = metadata.begin(pts, size, idr);
    if (descriptor.length != 96) throw new IOException("descriptor size");
    if (firstPts < 0) firstPts = pts;
    long ts = (Integer.toUnsignedLong(timestampBase) + (pts - firstPts) * 90 / 1000) & 0xffffffffL;
    for (int i = 0; i < nals.size(); i++) nal(nals.get(i), ts, i == nals.size() - 1);
    metadata.complete();
    accessUnits++;
  }

  private void nal(ByteBuffer n, long ts, boolean marker) throws IOException {
    if (n.limit() <= maxPayload) {
      writeHeader(ts, marker);
      packetWriter.put(n.duplicate());
      sendPacket();
      return;
    }
    ByteBuffer fragment = n.duplicate();
    for (int o = 1; o < n.limit(); ) {
      int count = Math.min(maxPayload - 2, n.limit() - o);
      boolean first = o == 1, last = o + count == n.limit();
      writeHeader(ts, marker && last);
      packetWriter.put((byte) ((n.get(0) & 0xe0) | 28));
      packetWriter.put((byte) ((n.get(0) & 31) | (first ? 128 : 0) | (last ? 64 : 0)));
      fragment.limit(o + count);
      fragment.position(o);
      packetWriter.put(fragment);
      sendPacket();
      o += count;
    }
  }

  // Every fragment carries the same 96-byte descriptor and a distinct RTP sequence.
  private void writeHeader(long ts, boolean marker) {
    packetWriter.clear();
    packetWriter.put((byte) 0x90)
        .put((byte) (pt | (marker ? 128 : 0)))
        .putShort((short) sequence)
        .putInt((int) ts)
        .putInt(ssrc)
        .putShort((short) 0x1000)
        .putShort((short) 25)
        .put((byte) 1)
        .put((byte) 96)
        .put(descriptor)
        .putShort((short) 0);
  }

  private void sendPacket() throws IOException {
    int length = packetWriter.position();
    sink.send(packetBuffer, length);
    sequence = (sequence + 1) & 65535;
    bytes += length;
    packets++;
  }
}
