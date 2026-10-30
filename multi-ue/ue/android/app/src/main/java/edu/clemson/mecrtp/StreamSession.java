package edu.clemson.mecrtp;

import android.net.Network;
import java.net.*;
import java.util.*;
import org.json.*;

/** Single session owner; frame identity never resets on control reconnection. */
public final class StreamSession implements H264Rtp.Metadata, H264Rtp.Sink {
  public interface Events {
    void event(String type, JSONObject value);
  }

  private final Events events;
  private final ClockMap clock;
  private final UUID video;
  private final long epoch;
  private final String host;
  private final int port;
  private final boolean collectionMode;
  private DatagramSocket socket;
  private Network network;
  private JSONObject current;
  private final List<JSONObject> pending = new ArrayList<>();
  // Encoder PTS links raw-frame identity to its eventual compressed access unit.
  private final Map<Long, long[]> input = new HashMap<>();
  private final Map<Long, long[]> sent = new LinkedHashMap<>();
  private long frame, gop, gopFrame, last = -1, nextRelease, pauseAt;
  private boolean force;
  private long controlVersion, pauseVersion;

  public StreamSession(JSONObject response, ClockMap clock, Events events, java.io.File data)
      throws Exception {
    this.events = events;
    this.clock = clock;
    video = UUID.fromString(response.getString("video_id"));
    epoch = response.getLong("media_epoch");
    host = response.getJSONObject("endpoint").getString("host");
    port = response.getJSONObject("endpoint").getInt("port");
    collectionMode = response.optBoolean("collection_mode", false);
    current = response.getJSONObject("config");
    controlVersion = current.optLong("control_version", 0);
    nextRelease = current.getLong("boundary_unix_ns");
  }

  public synchronized void network(Network n) throws Exception {
    network = n;
    if (socket != null) socket.close();
    socket = new DatagramSocket();
    if (n != null) n.bindSocket(socket);
    socket.connect(InetAddress.getByName(host), port);
  }

  public synchronized JSONObject config() {
    return current;
  }

  public boolean collectionMode() {
    return collectionMode;
  }

  // Retain future commands for the media path; arrival alone does not apply them.
  public synchronized void command(JSONObject c) throws Exception {
    if (c.optLong("control_version", 0) <= controlVersion) return;
    if (c.getInt("config_version") <= current.getInt("config_version")) return;
    for (JSONObject p : pending)
      if (p.getInt("config_version") == c.getInt("config_version")) return;
    if (c.getLong("boundary_unix_ns") <= clock.now()) {
      events.event(
          "plan_unapplied",
          new JSONObject()
              .put("command_id", c.getString("command_id"))
              .put("reason", "late_command"));
      return;
    }
    controlVersion = c.getLong("control_version");
    // A reordered resume carries the preceding stop even if its status arrived late.
    if (!c.isNull("stop_unix_ns") && c.getLong("suspension_version") > pauseVersion) {
      pauseAt = c.getLong("stop_unix_ns");
      pauseVersion = c.getLong("suspension_version");
    }
    pending.add(c);
    pending.sort(Comparator.comparingLong(x -> x.optLong("boundary_unix_ns")));
  }

  /** Return false before the first boundary, on suspension, or while an AU is in flight. */
  public synchronized boolean prepare(long pts, long capture) throws Exception {
    long now = clock.now();
    if (now < nextRelease || !input.isEmpty()) return false;
    force = false;
    while (!pending.isEmpty() && pending.get(0).getLong("boundary_unix_ns") <= now) {
      JSONObject c = pending.remove(0);
      if (now - c.getLong("boundary_unix_ns") > 1_000_000_000L / current.getInt("fps")) {
        events.event(
            "plan_unapplied",
            new JSONObject()
                .put("command_id", c.getString("command_id"))
                .put("reason", "boundary_missed"));
        continue;
      }
      current = c;
      nextRelease = c.getLong("boundary_unix_ns");
      gop++;
      gopFrame = 0;
      force = true;
      if (c.optLong("control_version", 0) > pauseVersion) pauseAt = 0;
    }
    if (pauseAt != 0 && now >= pauseAt) {
      // Preserve source-frame time while paused so the resumed frame ID exposes
      // the gap that ByteTrack must age through.
      frame++;
      nextRelease += 1_000_000_000L / current.getInt("fps");
      return false;
    }
    input.put(pts, new long[] {capture, frame, gop, gopFrame, force ? 1 : 0});
    nextRelease += 1_000_000_000L / current.getInt("fps");
    return true;
  }

  public synchronized boolean keyframe() {
    return force || gopFrame == 0;
  }

  public synchronized void suspend(JSONObject command) throws Exception {
    long version = command.getLong("control_version");
    if (version <= controlVersion) return;
    controlVersion = version;
    pauseVersion = version;
    pauseAt = command.getLong("effective_unix_ns");
    events.event("suspension", command);
  }

  @Override
  // Validate the requested IDR before labeling and transmitting the encoded frame.
  public synchronized byte[] begin(long pts, long size, boolean idr) throws java.io.IOException {
    try {
      long[] in = input.remove(pts);
      if (in == null) throw new Exception("No raw-frame metadata for encoder output");
      if ((in[3] == 0 || in[4] == 1) && !idr)
        throw new Exception("Encoder did not produce commanded IDR");
      long tx = System.nanoTime();
      boolean valid = clock.valid();
      long fid = in[1];
      sent.put(fid, new long[] {tx, 0, 0});
      while (sent.size() > 4096) sent.remove(sent.keySet().iterator().next());
      last = fid;
      events.event(
          "transmitted",
          new JSONObject()
              .put("video_id", video)
              .put("frame_id", fid)
              .put("tx_mono_ns", tx)
              .put("capture_mono_ns", in[0])
              .put("encoded_bytes", size)
              .put("config_version", current.getInt("config_version"))
              .put("plan_version", current.getInt("plan_version")));
      frame++;
      gopFrame = (gopFrame + 1) % current.getInt("gop_frames");
      if (gopFrame == 0) gop++;
      return FrameDescriptor.encode(
          video,
          epoch,
          fid,
          in[2],
          current.getInt("config_version"),
          current.getInt("plan_version"),
          idr ? 1 : 3,
          size,
          in[0],
          tx,
          valid ? clock.unix(in[0]) : 0,
          valid ? clock.unix(tx) : 0,
          valid ? 15 : 3);
    } catch (Exception e) {
      throw new java.io.IOException(e);
    }
  }

  @Override
  public synchronized void send(byte[] packet) throws java.io.IOException {
    if (socket == null) throw new java.io.IOException("Network unavailable");
    socket.send(new DatagramPacket(packet, packet.length));
  }

  // SLO evidence ends at complete UE receipt, using the same clock as transmission.
  public synchronized void receipt(JSONObject m, long received) throws Exception {
    String id = m.getString("result_id");
    long fid = m.getLong("frame_id");
    long[] t = sent.get(fid);
    long latency = t == null ? -1 : received - t[0];
    if (t != null) t[1] = received;
    events.event(
        "result_receipt",
        new JSONObject()
            .put("campaign_id", m.opt("campaign_id"))
            .put("scenario_id", m.opt("scenario_id"))
            .put("attempt_id", m.opt("attempt_id"))
            .put("video_id", video)
            .put("frame_id", fid)
            .put("result_id", id)
            .put("latency_ns", latency < 0 ? JSONObject.NULL : latency)
            .put("receipt_mono_ns", received)
            .put("outcome", m.getString("outcome"))
            .put(
                "slo_success",
                latency >= 0
                    && latency <= 150_000_000L
                    && m.getString("outcome").equals("completed")));
  }

  // Missing-result timers run independently of MEC success or failure notifications.
  public synchronized void deadlines() throws Exception {
    long now = System.nanoTime();
    java.util.Iterator<Map.Entry<Long, long[]>> iterator = sent.entrySet().iterator();
    while (iterator.hasNext()) {
      Map.Entry<Long, long[]> x = iterator.next();
      long[] t = x.getValue();
      if (t[1] == 0 && t[2] == 0 && now - t[0] >= 150_000_000L) {
        t[2] = 1;
        events.event(
            "deadline_miss", new JSONObject().put("video_id", video).put("frame_id", x.getKey()));
      }
      if (t[1] == 0 && t[2] == 1 && now - t[0] >= 5_150_000_000L) {
        if (collectionMode) continue;
        t[2] = 2;
        events.event(
            "final_missing", new JSONObject().put("video_id", video).put("frame_id", x.getKey()));
      }
      if (t[1] != 0 || (!collectionMode && now - t[0] >= 5_150_000_000L))
        iterator.remove();
    }
  }

  public long clockNow() {
    return clock.now();
  }

  /** Begin a prepared file at its planned boundary or now if local setup ran late. */
  public synchronized long activate() {
    nextRelease = Math.max(nextRelease, clock.now());
    return nextRelease;
  }

  public String video() {
    return video.toString();
  }

  public synchronized long lastFrame() {
    return last;
  }

  public synchronized void close() {
    if (socket != null) socket.close();
  }
}
