import edu.clemson.mecrtp.OrientationSample;
import java.nio.file.*;
import java.util.*;

public class OrientationSampleGolden {
  public static void main(String[] args) throws Exception {
    OrientationSample sample = new OrientationSample();
    try {
      sample.add(Arrays.asList(new byte[] {0x65, 1}));
      throw new AssertionError("Missing parameter sets accepted");
    } catch (java.io.IOException expected) { }
    sample.add(Arrays.asList(new byte[] {0x67, 1}, new byte[] {0x68, 2}, new byte[] {0x65, 3}));
    for (int i = 1; i < 35; i++) sample.add(Arrays.asList(new byte[] {0x41, 4}));
    if (sample.frames() != 30) throw new AssertionError("Sample frame limit");
    Path file = Files.createTempFile("orientation-sample-", ".h264");
    try {
      sample.save(file.toFile());
      byte[] data = Files.readAllBytes(file);
      if (data.length != 18 + 29 * 6 || data[3] != 1 || data[4] != 0x67)
        throw new AssertionError("Invalid Annex-B capture");
    } finally { Files.delete(file); }
    System.out.println("Orientation sample headers and 30-frame limit: PASS");
  }
}
