import edu.clemson.mecrtp.MjpegReader;
import edu.clemson.mecrtp.YuvScaler;
import edu.clemson.mecrtp.H264Rtp;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.file.*;
import java.util.*;

public class MediaUtilitiesTest {
    static void check(boolean ok,String detail){if(!ok)throw new AssertionError(detail);}
    public static void main(String[] args)throws Exception{
        ByteBuffer source=ByteBuffer.allocate(70),target=ByteBuffer.allocate(40);
        source.position(2);target.position(3);
        for(int y=0;y<4;y++)for(int x=0;x<6;x++)source.put(2+y*16+x*2,(byte)(10*y+x));
        YuvScaler.plane(source,16,2,target,10,2,2,0,4,4,2,2);
        check(target.get(3)==2&&target.get(5)==4&&target.get(13)==22&&target.get(15)==24,"Crop/resize preserves stride/pixel offsets");
        YuvScaler.validateCrop(1280,720,2,4,6,8);
        for(int[] crop:new int[][]{{1,0,0,0},{1280,0,0,0},{0,0,-2,0}}){
            try{YuvScaler.validateCrop(1280,720,crop[0],crop[1],crop[2],crop[3]);throw new AssertionError("Invalid crop accepted");}
            catch(IllegalArgumentException expected){}
        }
        byte[] jpeg={(byte)255,(byte)216,0,10,13,45,45,102,114,97,109,101,(byte)255,(byte)217};
        ByteArrayOutputStream body=new ByteArrayOutputStream();
        body.write(("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+jpeg.length+"\r\n\r\n").getBytes("US-ASCII"));body.write(jpeg);body.write("\r\n--frame\r\n".getBytes("US-ASCII"));
        check(Arrays.equals(jpeg,MjpegReader.firstFrame(new ByteArrayInputStream(body.toByteArray()))),"Binary MJPEG Content-Length parsing");
        try{MjpegReader.firstFrame(new ByteArrayInputStream("--frame\r\nContent-Length: 9000000\r\n\r\n".getBytes()));throw new AssertionError("Oversized JPEG accepted");}
        catch(IOException expected){}
        if(args.length>0){byte[] actual=MjpegReader.firstFrame(Files.newInputStream(Path.of(args[0])));check(actual[0]==(byte)255,"Actual unchanged server multipart fixture");}
        List<byte[]> packets=new ArrayList<>();H264Rtp small=new H264Rtp(packets::add,96,576);
        byte[] nal=new byte[3000];Arrays.fill(nal,(byte)1);nal[0]=0x65;small.accessUnit(nal,0);
        check(packets.size()>1&&packets.stream().allMatch(p->p.length<=576),"Configured RTP MTU respected");
        System.out.println("Media utilities passed: raw crop/resize, stride handling, MJPEG parsing, supplied-server fixture, configurable RTP MTU");
    }
}
