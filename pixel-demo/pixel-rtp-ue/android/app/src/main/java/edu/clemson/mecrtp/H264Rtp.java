package edu.clemson.mecrtp;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;

/** RFC 6184 non-interleaved mode: single NAL and FU-A. No Android dependencies. */
public final class H264Rtp {
    public interface Sink { void send(byte[] packet) throws IOException; }
    private final Sink sink;
    private final int payloadType, ssrc, timestampBase;
    private int sequence;
    private long firstPtsUs = -1;
    private final List<byte[]> parameterSets = new ArrayList<>();
    public long bytes, packets, accessUnits;
    // Configured limit includes the RTP header; default 1200 leaves tunnel headroom.
    private final int maxPayload;

    public H264Rtp(Sink sink, int payloadType) {
        this(sink, payloadType, 1200);
    }
    public H264Rtp(Sink sink, int payloadType, int mtu) {
        if (mtu < 576 || mtu > 1400) throw new IllegalArgumentException("RTP MTU must be 576-1400");
        maxPayload = mtu - 12;
        if (payloadType < 96 || payloadType > 127) throw new IllegalArgumentException("Dynamic RTP PT required");
        this.sink = sink; this.payloadType = payloadType;
        Random r = new Random(); ssrc = r.nextInt(); timestampBase = r.nextInt(); sequence = r.nextInt(65536);
    }

    /** MediaCodec normally emits Annex B; accept four-byte length prefixes as well. */
    public static List<byte[]> split(byte[] data) {
        List<byte[]> result = new ArrayList<>();
        int first = startCode(data, 0);
        if (first >= 0 && first <= 3) {
            int pos = first;
            while (pos >= 0) {
                int prefix = data[pos + 2] == 1 ? 3 : 4;
                int begin = pos + prefix;
                int next = startCode(data, begin);
                int end = next < 0 ? data.length : next;
                if (end > begin) result.add(Arrays.copyOfRange(data, begin, end));
                pos = next;
            }
        } else {
            int pos = 0;
            while (pos + 4 <= data.length) {
                int len = ByteBuffer.wrap(data, pos, 4).getInt(); pos += 4;
                if (len <= 0 || len > data.length - pos) { result.clear(); break; }
                result.add(Arrays.copyOfRange(data, pos, pos + len)); pos += len;
            }
            if (pos != data.length) result.clear();
            if (result.isEmpty() && data.length > 0) result.add(data.clone());
        }
        return result;
    }
    private static int startCode(byte[] d, int from) {
        for (int i = from; i + 2 < d.length; i++) {
            if (d[i] == 0 && d[i + 1] == 0 && (d[i + 2] == 1 ||
                (i + 3 < d.length && d[i + 2] == 0 && d[i + 3] == 1))) return i;
        }
        return -1;
    }
    public void configure(byte[] bytes) {
        for (byte[] nal : split(bytes)) {
            int type = nal[0] & 31;
            if (type == 7 || type == 8) {
                parameterSets.removeIf(old -> (old[0] & 31) == type);
                parameterSets.add(nal);
            }
        }
    }
    public void accessUnit(byte[] encoded, long ptsUs) throws IOException {
        List<byte[]> nals = split(encoded);
        boolean idr = false;
        for (byte[] nal : nals) { if ((nal[0] & 31) == 5) idr = true; }
        configure(encoded);
        if (firstPtsUs < 0) firstPtsUs = ptsUs;
        long ts = (Integer.toUnsignedLong(timestampBase) + (ptsUs - firstPtsUs) * 90 / 1000) & 0xffffffffL;
        // Repeat SPS/PPS ahead of each IDR so receivers can join/recover without SDP sprop.
        if (idr) for (byte[] nal : parameterSets) nal(nal, ts, false);
        for (int i = 0; i < nals.size(); i++) nal(nals.get(i), ts, i == nals.size() - 1);
        accessUnits++;
    }
    private void nal(byte[] nal, long ts, boolean marker) throws IOException {
        int type = nal[0] & 31;
        if (type < 1 || type > 23) throw new IOException("Unexpected H.264 NAL type " + type);
        if (nal.length <= maxPayload) { packet(nal, ts, marker); return; }
        int offset = 1;
        while (offset < nal.length) {
            int count = Math.min(maxPayload - 2, nal.length - offset);
            boolean first = offset == 1, last = offset + count == nal.length;
            byte[] fu = new byte[count + 2];
            fu[0] = (byte)((nal[0] & 0xe0) | 28);
            fu[1] = (byte)(type | (first ? 0x80 : 0) | (last ? 0x40 : 0));
            System.arraycopy(nal, offset, fu, 2, count);
            packet(fu, ts, marker && last); offset += count;
        }
    }
    private void packet(byte[] payload, long ts, boolean marker) throws IOException {
        byte[] p = new byte[payload.length + 12];
        ByteBuffer b = ByteBuffer.wrap(p);
        b.put((byte)0x80).put((byte)(payloadType | (marker ? 0x80 : 0)));
        b.putShort((short)sequence).putInt((int)ts).putInt(ssrc).put(payload);
        sink.send(p); sequence = (sequence + 1) & 65535; bytes += p.length; packets++;
    }
}
