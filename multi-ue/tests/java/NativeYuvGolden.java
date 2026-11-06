import edu.clemson.mecrtp.NativeYuv;
import java.nio.ByteBuffer;
import java.nio.file.*;
import java.util.Arrays;

/** Host JNI smoke test and planar reference-file converter for real-video checks. */
public class NativeYuvGolden {
  public static void main(String[] args) throws Exception {
    if (args.length != 0) {
      byte[] source = Files.readAllBytes(Path.of(args[0]));
      int w=Integer.parseInt(args[2]), h=Integer.parseInt(args[3]), rotation=Integer.parseInt(args[4]);
      int dw=rotation%180 == 0 ? w:h, dh=rotation%180 == 0 ? h:w;
      byte[] output = new byte[source.length]; int offset=0;
      for(int p=0;p<3;p++) {
        int scale=p==0?1:2, sw=w/scale, sh=h/scale, tw=dw/scale, th=dh/scale;
        ByteBuffer s=ByteBuffer.allocateDirect(sw*sh), d=ByteBuffer.allocateDirect(tw*th);
        s.put(source,offset,sw*sh).flip();
        NativeYuv.plane(s,sw,1,0,0,sw,sh,d,tw,1,0,0,tw,th,rotation);
        d.get(output,offset,tw*th); offset+=sw*sh;
      }
      Files.write(Path.of(args[1]),output);
      return;
    }
    ByteBuffer s=ByteBuffer.allocateDirect(32), d=ByteBuffer.allocateDirect(32);
    s.position(5); s.put(new byte[]{1,2,3,4,5,6}); s.limit(11).position(5);
    d.position(7).limit(13);
    NativeYuv.plane(s,2,1,0,0,2,3,d,3,1,0,0,3,2,90);
    byte[] actual=new byte[6]; d.duplicate().get(actual);
    if(!Arrays.equals(actual,new byte[]{5,3,1,6,4,2}) || s.position()!=5 || d.position()!=7)
      throw new AssertionError("JNI rotation or buffer positions");
    try {
      NativeYuv.plane(s,2,1,0,0,2,3,d.asReadOnlyBuffer(),3,1,0,0,3,2,90);
      throw new AssertionError("Read-only output accepted");
    } catch(IllegalArgumentException expected) {}
    d.limit(12);
    try {
      NativeYuv.plane(s,2,1,0,0,2,3,d,3,1,0,0,3,2,90);
      throw new AssertionError("Short output accepted");
    } catch(IllegalArgumentException expected) {}
    System.out.println("Native JNI buffer bounds, positions, rotation, and read-only protection: PASS");
  }
}
