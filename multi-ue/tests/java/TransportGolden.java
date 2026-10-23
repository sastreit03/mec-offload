import edu.clemson.mecrtp.FrameDescriptor;
import edu.clemson.mecrtp.H264Rtp;
import java.util.UUID;
import java.util.Base64;
// Emit a deterministic Java transport fixture for comparison with the Python parser.
public class TransportGolden {
 public static void main(String[] args)throws Exception {
  byte[] data=new byte[5005];data[3]=1;data[4]=0x65;java.util.Arrays.fill(data,5,data.length,(byte)42);
  H264Rtp r=new H264Rtp(p->System.out.println(Base64.getEncoder().encodeToString(p)),96,1200,(pts,size,idr)->FrameDescriptor.encode(UUID.fromString("00000000-0000-0000-0000-000000000001"),1,2,3,4,5,idr?1:3,size,10,20,30,40,15));r.accessUnit(data,0);
 }
}
