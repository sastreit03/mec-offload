package edu.clemson.mecrtp;

public final class ClockMap {
  private long offset, uncertainty = Long.MAX_VALUE, updated;

  public synchronized void observe(long t0, long t1, long t2, long t3) {
    long u = Math.max(0, ((t3 - t0) - (t2 - t1)) / 2);
    if (u < uncertainty || System.nanoTime() - updated > 30_000_000_000L) {
      offset = ((t1 - t0) + (t2 - t3)) / 2;
      uncertainty = u;
      updated = System.nanoTime();
    }
  }

  public synchronized boolean valid() {
    return updated != 0 && System.nanoTime() - updated < 60_000_000_000L;
  }

  public synchronized long unix(long mono) {
    return mono + offset;
  }

  public synchronized long now() {
    return valid() ? unix(System.nanoTime()) : System.currentTimeMillis() * 1_000_000L;
  }

  public synchronized long uncertainty() {
    return uncertainty;
  }

  public synchronized long offset() {
    return offset;
  }
}
