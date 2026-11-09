import edu.clemson.mecrtp.H264Rtp;
import java.util.List;

/** Pure-Java regression: offline size accounting must match production RTP metadata. */
public class EncoderPayloadGolden {
  public static void main(String[] args) throws Exception {
    byte[] config = {0,0,0,1,0x67,42,0,0,0,1,0x68,43};
    byte[] idr = {0,0,0,1,0x65,44,45};
    byte[] predicted = {0,0,0,1,0x41,46};
    long[] metadata = {-1};
    int[] packets = {0};
    H264Rtp rtp = new H264Rtp((p, length) -> packets[0]++, 96, 1200,
        (pts, size, keyframe) -> { metadata[0] = size; return new byte[96]; });
    rtp.configure(config);
    List<byte[]> nals = rtp.accessUnitNals(idr);
    long size = nals.stream().mapToLong(n -> n.length + 4).sum();
    if (size != 19 || nals.size() != 3 || packets[0] != 0)
      throw new AssertionError("Offline IDR accounting or unexpected transmission");
    rtp.accessUnit(idr, 0);
    if (metadata[0] != size || packets[0] != 3)
      throw new AssertionError("RTP size differs from offline size");
    nals = rtp.accessUnitNals(predicted);
    size = nals.stream().mapToLong(n -> n.length + 4).sum();
    if (size != 6 || nals.size() != 1) throw new AssertionError("P-frame has extra parameter sets");
    rtp.accessUnit(predicted, 100000);
    if (metadata[0] != size) throw new AssertionError("P-frame size mismatch");
    // In-band parameter updates must replace the cache without duplicating SPS/PPS.
    byte[] updated = {0,0,0,1,0x67,50,0,0,0,1,0x68,51,0,0,0,1,0x65,52};
    nals = rtp.accessUnitNals(updated);
    if (nals.size() != 3 || nals.get(0)[1] != 50 || nals.get(1)[1] != 51)
      throw new AssertionError("In-band parameter update changed payload order");
    nals = rtp.accessUnitNals(idr);
    if (nals.size() != 3 || nals.get(0)[1] != 50 || nals.get(1)[1] != 51)
      throw new AssertionError("Updated parameters were not retained for the next IDR");
    System.out.println("Encoder payload accounting: PASS");
  }
}
