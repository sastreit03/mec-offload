import edu.clemson.mecrtp.H264Rtp;
import java.io.ByteArrayOutputStream;
import java.net.*;
import java.nio.file.*;
import java.util.*;

/** Send an Annex-B fixture with AUD delimiters, using the actual production packetizer. */
public class RtpFileSender {
    public static void main(String[] args) throws Exception {
        DatagramSocket socket = new DatagramSocket();
        socket.connect(InetAddress.getLoopbackAddress(), Integer.parseInt(args[1]));
        H264Rtp sender = new H264Rtp(p -> socket.send(new DatagramPacket(p,p.length)),96);
        List<byte[]> nals = H264Rtp.split(Files.readAllBytes(Path.of(args[0])));
        ByteArrayOutputStream au = new ByteArrayOutputStream(); long pts=0;
        for (byte[] nal : nals) {
            if ((nal[0]&31)==9 && au.size()>0) {
                sender.accessUnit(au.toByteArray(),pts); pts+=66667; Thread.sleep(67); au.reset();
            }
            au.write(new byte[]{0,0,0,1}); au.write(nal);
        }
        if(au.size()>0)sender.accessUnit(au.toByteArray(),pts);
        System.out.println("Sent "+sender.accessUnits+" access units, "+sender.packets+" RTP packets");
        socket.close();
    }
}
