package edu.clemson.mecrtp;

import java.nio.ByteBuffer;
import java.util.UUID;

/** Shared v1 descriptor: unsigned network-order fields, 96 bytes. */
public final class FrameDescriptor {
  public static byte[] encode(
      UUID video,
      long epoch,
      long frame,
      long gop,
      int config,
      int plan,
      int type,
      long bytes,
      long capture,
      long tx,
      long captureUnix,
      long txUnix,
      int flags) {
    // Configuration boundaries use IDR frames; non-IDR I and B frames are unsupported.
    if (type != 1 && type != 3)
      throw new IllegalArgumentException("frame type must be IDR(1) or P(3)");
    // ByteBuffer defaults to big endian; keep offsets identical to Python DESCRIPTOR.
    return ByteBuffer.allocate(96)
        .put((byte) 1)
        .put((byte) flags)
        .putShort((short) 0)
        .putLong(video.getMostSignificantBits())
        .putLong(video.getLeastSignificantBits())
        .putLong(epoch)
        .putLong(frame)
        .putLong(gop)
        .putInt(config)
        .putInt(plan)
        .put((byte) type)
        .put(new byte[3])
        .putLong(bytes)
        .putLong(capture)
        .putLong(tx)
        .putLong(captureUnix)
        .putLong(txUnix)
        .array();
  }
}
