import edu.clemson.mecrtp.H264Rtp;
import java.nio.ByteBuffer;
import java.util.*;

/** Run with a JDK, without an Android SDK: javac -d build ...; java -ea -cp build RtpProtocolTest. */
public class RtpProtocolTest {
    static void check(boolean value, String message) { if (!value) throw new AssertionError(message); }
    static byte[] annex(byte[]... nals) {
        int size = 0; for (byte[] n : nals) size += 4 + n.length;
        ByteBuffer b = ByteBuffer.allocate(size);
        for (byte[] n : nals) b.putInt(1).put(n);
        return b.array();
    }
    static long timestamp(byte[] p) { return Integer.toUnsignedLong(ByteBuffer.wrap(p, 4, 4).getInt()); }
    public static void main(String[] args) throws Exception {
        List<byte[]> sent = new ArrayList<>(); H264Rtp rtp = new H264Rtp(sent::add, 96);
        byte[] sps = {0x67, 0x42, 0, 31}, pps = {0x68, 1, 2}, idr = new byte[5000];
        new Random(5).nextBytes(idr); idr[0] = 0x65;
        // Avoid accidental Annex-B start codes inside this artificial NAL.
        for (int i=1; i<idr.length; i++) if (idr[i]==0) idr[i]=1;
        rtp.configure(annex(sps, pps)); rtp.accessUnit(annex(idr), 1000000);
        check(sent.size() == 7, "SPS, PPS and five FU-A fragments expected");
        int previous = -1; long ts = timestamp(sent.get(0));
        ByteBuffer reconstructed = ByteBuffer.allocate(idr.length); reconstructed.put((byte)0x65);
        for (int i = 0; i < sent.size(); i++) {
            byte[] p = sent.get(i); check(p.length <= 1200, "MTU bound");
            check((p[0]&255)==0x80 && (p[1]&127)==96, "RTP v2/PT96");
            int seq = Short.toUnsignedInt(ByteBuffer.wrap(p, 2, 2).getShort());
            if(previous>=0)check(seq==((previous+1)&65535), "consecutive sequence"); previous=seq;
            check(timestamp(p)==ts, "same access-unit timestamp");
            check((p[1]&128)!=0 == (i==sent.size()-1), "marker only on final AU packet");
            if(i>=2) {
                check((p[12]&31)==28 && (p[13]&31)==5, "FU-A type");
                check(((p[13]&128)!=0)==(i==2), "FU start");
                check(((p[13]&64)!=0)==(i==sent.size()-1), "FU end");
                reconstructed.put(p,14,p.length-14);
            }
        }
        check(Arrays.equals(reconstructed.array(),idr), "lossless FU-A reconstruction");
        int before=sent.size(); rtp.accessUnit(annex(new byte[]{0x41,3,4}),1066667);
        check(sent.size()==before+1,"single NAL");
        check(((timestamp(sent.get(before))-ts)&0xffffffffL)==6000,"90 kHz timestamp from presentation time");
        byte[] avcc=ByteBuffer.allocate(4+sps.length+4+pps.length).putInt(sps.length).put(sps).putInt(pps.length).put(pps).array();
        List<byte[]> split=H264Rtp.split(avcc);
        check(split.size()==2&&Arrays.equals(split.get(0),sps)&&Arrays.equals(split.get(1),pps),"AVCC framing");
        check(H264Rtp.split(new byte[]{0,0,1,0x41,1,0,0,0,1,0x41,2}).size()==2,"mixed Annex B prefixes");
        check(rtp.packets==sent.size()&&rtp.accessUnits==2,"sender counters");
        System.out.println("RTP tests passed: SPS/PPS, FU-A, markers, timestamps, sequences, framing, MTU");
    }
}
