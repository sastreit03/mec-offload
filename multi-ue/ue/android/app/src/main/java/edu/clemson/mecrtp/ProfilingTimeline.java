package edu.clemson.mecrtp;

/** Original decoder PTS never controls the retimed encoder or pacing timeline. */
public final class ProfilingTimeline {
  private ProfilingTimeline() {}
  public static long ptsUs(int frame, int fps) { return Math.round(frame * 1_000_000.0 / fps); }
  public static long releaseNs(int frame, int fps) { return Math.round(frame * 1_000_000_000.0 / fps); }
}
