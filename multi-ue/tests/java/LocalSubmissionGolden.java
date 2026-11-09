import edu.clemson.mecrtp.*;
import java.util.*;

/** Real UDP test of the unchanged packetizer's construct/send path and retiming. */
public class LocalSubmissionGolden {
  public static void main(String[] args) throws Exception {
    if (ProfilingTimeline.ptsUs(1, 30) != 33333 || ProfilingTimeline.ptsUs(2, 30) != 66667
        || ProfilingTimeline.ptsUs(1200, 10) != 120000000
        || ProfilingTimeline.releaseNs(1200, 30) != 40000000000L)
      throw new AssertionError("Retiming mismatch");
    UUID video = UUID.randomUUID();
    long[] fid = {0};
    try (ProfilingLoopback loop = new ProfilingLoopback()) {
      // Drop one fragment of frame 1 to prove incomplete receipt is detected.
      int[] packetIndex = {0};
      H264Rtp rtp = new H264Rtp((p, length) -> {
        if (fid[0] == 1 && packetIndex[0]++ == 3) return;
        loop.send(p, length);
      }, 96, 1200, (pts, bytes, idr) -> FrameDescriptor.encode(
          video, 1, fid[0], 0, 1, 1, idr ? 1 : 3, bytes, 0, 0, 0, 0, 0));
      rtp.configure(new byte[] {0,0,0,1,0x67,42,0,0,0,1,0x68,43});
      byte[] frame = new byte[4005];
      Arrays.fill(frame, (byte) 44);
      frame[0]=frame[1]=frame[2]=0; frame[3]=1; frame[4]=0x65;
      long[] counts = new long[2], bytes = new long[2]; byte[][] hashes = new byte[2][];
      for (int i=0; i<2; i++) {
        fid[0]=i; loop.beginFrame();
        rtp.accessUnit(frame, ProfilingTimeline.ptsUs(i, 10));
        counts[i]=loop.packets; bytes[i]=loop.bytes; hashes[i]=loop.sentHash();
        if (loop.socketNs <= 0 || loop.lastSendNs < loop.firstSendNs)
          throw new AssertionError("Missing socket timing");
      }
      Map<Long, ProfilingLoopback.Receipt> received = loop.finish(counts[0]+counts[1]);
      if (!ProfilingLoopback.verified(received.get(0L), counts[0], bytes[0], hashes[0]))
        throw new AssertionError("Valid fragmented frame failed verification");
      if (ProfilingLoopback.verified(received.get(1L), counts[1], bytes[1], hashes[1]))
        throw new AssertionError("Missing fragment passed verification");
    }
    System.out.println("Pixel loopback packetization, receipts, and retiming: PASS");
  }
}
