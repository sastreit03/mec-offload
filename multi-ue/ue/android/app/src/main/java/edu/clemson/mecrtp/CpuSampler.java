package edu.clemson.mecrtp;

import android.os.Process;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import org.json.JSONException;
import org.json.JSONObject;

/** One process CPU counter per handset and attempt, including the result drain. */
final class CpuSampler {
  private final ScheduledExecutorService timer;
  private final StreamSession.Events events;
  private ScheduledFuture<?> periodic;
  private Runnable current;

  CpuSampler(ScheduledExecutorService timer, StreamSession.Events events) {
    this.timer = timer;
    this.events = events;
  }

  synchronized void start(String campaign, String scenario, String attempt) {
    stop();
    Runnable sample = () -> {
      long mono = System.nanoTime();
      long cpuMs = Process.getElapsedCpuTime();
      try {
        events.event("cpu_sample", new JSONObject().put("campaign_id", campaign)
            .put("scenario_id", scenario).put("attempt_id", attempt)
            .put("sample_mono_ns", mono).put("process_cpu_ms", cpuMs));
      } catch (JSONException e) {
        throw new IllegalStateException(e);
      }
    };
    current = sample;
    sample.run();
    periodic = timer.scheduleWithFixedDelay(() -> {
      synchronized (CpuSampler.this) {
        if (current == sample) sample.run();
      }
    }, 250, 250, TimeUnit.MILLISECONDS);
  }

  synchronized void stop() {
    if (current == null) return;
    periodic.cancel(false);
    current.run(); // Cover the final, usually partial sampling window.
    current = null;
  }
}
