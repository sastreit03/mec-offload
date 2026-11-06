package edu.clemson.mecrtp;

import java.io.IOException;
import java.net.*;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.util.*;

/** Profiling-only loopback sink; no remote address is accepted. */
public final class ProfilingLoopback implements AutoCloseable {
  public static final class Receipt {
    public long packets, bytes, firstNs, lastNs, canonicalBytes, expectedBytes;
    public boolean marker;
    final MessageDigest digest = digest();
    byte[] descriptor;
  }
  private final DatagramSocket receiver, sender;
  private final Thread thread;
  private final Map<Long, Receipt> received = new HashMap<>();
  private volatile boolean stopped;
  private volatile long receivedPackets;
  private volatile IOException error;
  public long socketNs, verificationNs, firstSendNs, lastSendNs, packets, bytes;
  private MessageDigest sentDigest = digest();

  private static MessageDigest digest() {
    try { return MessageDigest.getInstance("SHA-256"); }
    catch (Exception e) { throw new IllegalStateException(e); }
  }

  public ProfilingLoopback() throws IOException {
    InetAddress loopback = InetAddress.getByName("127.0.0.1");
    receiver = new DatagramSocket(new InetSocketAddress(loopback, 0));
    DatagramSocket created = null;
    try {
      receiver.setReceiveBufferSize(4 * 1024 * 1024);
      receiver.setSoTimeout(20);
      created = new DatagramSocket(new InetSocketAddress(loopback, 0));
      created.connect(loopback, receiver.getLocalPort());
      sender = created;
    } catch (IOException e) {
      receiver.close(); if (created != null) created.close(); throw e;
    }
    thread = new Thread(this::receive, "profiling-loopback-receiver");
    thread.start();
  }

  public int receiveBufferBytes() throws SocketException { return receiver.getReceiveBufferSize(); }
  public int sendBufferBytes() throws SocketException { return sender.getSendBufferSize(); }

  public void beginFrame() {
    socketNs = verificationNs = firstSendNs = lastSendNs = packets = bytes = 0;
    sentDigest = digest();
  }

  public void send(byte[] packet) throws IOException {
    DatagramPacket datagram = new DatagramPacket(packet, packet.length);
    long start = System.nanoTime();
    if (firstSendNs == 0) firstSendNs = start;
    sender.send(datagram);
    lastSendNs = System.nanoTime();
    socketNs += lastSendNs - start;
    long hashStart = System.nanoTime();
    sentDigest.update(packet);
    packets++; bytes += packet.length;
    verificationNs += System.nanoTime() - hashStart;
  }

  public byte[] sentHash() { return sentDigest.digest(); }

  private void receive() {
    byte[] buffer = new byte[65535];
    while (!stopped) {
      DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
      try {
        receiver.receive(packet);
        long now = System.nanoTime();
        byte[] raw = Arrays.copyOf(buffer, packet.getLength());
        // H264Rtp emits this fixed extension layout. Reject unexpected layouts.
        if (raw.length <= 116 || raw[0] != (byte) 0x90 || raw[12] != 0x10
            || raw[13] != 0 || raw[14] != 0 || raw[15] != 25 || raw[16] != 1 || raw[17] != 96)
          throw new IOException("Unexpected profiling RTP layout");
        ByteBuffer b = ByteBuffer.wrap(raw);
        long fid = b.getLong(18 + 28);
        Receipt r = received.computeIfAbsent(fid, ignored -> new Receipt());
        byte[] descriptor = Arrays.copyOfRange(raw, 18, 114);
        if (r.descriptor == null) { r.descriptor = descriptor; r.expectedBytes = b.getLong(18 + 56); }
        else if (!Arrays.equals(r.descriptor, descriptor)) throw new IOException("Conflicting descriptor");
        if (r.packets == 0) r.firstNs = now;
        r.lastNs = now; r.packets++; r.bytes += raw.length; r.digest.update(raw);
        int kind = raw[116] & 31;
        if (kind == 28) {
          if (raw.length < 119) throw new IOException("Short FU-A");
          r.canonicalBytes += raw.length - 118 + ((raw[117] & 128) != 0 ? 5 : 0);
        } else if (kind >= 1 && kind <= 23) r.canonicalBytes += raw.length - 116 + 4;
        else throw new IOException("Unsupported NAL");
        r.marker |= (raw[1] & 128) != 0;
        receivedPackets++;
      } catch (SocketTimeoutException ignored) {
      } catch (IOException e) { if (!stopped) error = e; return; }
    }
  }

  /** Call after all sends; receipt maps become readable only after receiver joins. */
  public Map<Long, Receipt> finish(long expectedPackets) throws Exception {
    long deadline = System.nanoTime() + 2_000_000_000L;
    while (receivedPackets < expectedPackets && error == null && System.nanoTime() < deadline)
      Thread.sleep(5);
    stopped = true;
    thread.join(1000);
    if (thread.isAlive()) throw new IOException("Loopback receiver failed to stop");
    if (error != null) throw error;
    return received;
  }

  public static boolean verified(Receipt r, long count, long byteCount, byte[] hash) {
    return r != null && r.marker && r.canonicalBytes == r.expectedBytes
        && r.packets == count && r.bytes == byteCount && Arrays.equals(r.digest.digest(), hash);
  }

  @Override public void close() {
    stopped = true; sender.close(); receiver.close();
    try { thread.join(1000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
  }
}
