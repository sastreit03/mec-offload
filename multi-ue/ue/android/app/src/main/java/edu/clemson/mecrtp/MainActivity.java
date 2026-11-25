package edu.clemson.mecrtp;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.net.*;
import android.os.*;
import android.provider.DocumentsContract;
import android.util.Log;
import android.widget.*;
import java.io.*;
import java.net.*;
import java.util.concurrent.*;
import okhttp3.*;
import org.json.*;

/** Foreground research client: cellular-bound control, media, automatic probes and recovery. */
public final class MainActivity extends Activity {
  private static final String TAG = "AetheredgeUE";
  private final Handler ui = new Handler(Looper.getMainLooper());
  private final ClockMap clock = new ClockMap();
  private final ScheduledExecutorService timer = Executors.newScheduledThreadPool(2);
  private final ExecutorService probes = Executors.newSingleThreadExecutor();
  private final ExecutorService networkIo = Executors.newSingleThreadExecutor();
  private final ThreadPoolExecutor log =
      new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(4096));
  // One reader preserves wire order across result and lifecycle messages.
  private final ThreadPoolExecutor incoming =
      new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new LinkedBlockingQueue<>());
  private BufferedWriter evidenceWriter; // Owned exclusively by the log executor.
  private final CpuSampler cpu = new CpuSampler(timer, this::event);
  private boolean refreshPending;
  private ConnectivityManager connectivity;
  private ConnectivityManager.NetworkCallback callback;
  private volatile Network network;
  private volatile WebSocket ws;
  private EditText host;
  private EditText ueAlias;
  private Spinner application;
  private TextView datasetStatus, controlStatus, frameStatus, status;
  private long lastTransmittedFrame = -1, lastResultFrame = -1;
  private String lastResultOutcome = "none";
  private Uri file;
  private VideoPlaylist playlist;
  private final ExecutorService datasetIo = Executors.newSingleThreadExecutor();
  private boolean datasetLoading;
  private volatile boolean destroyed;
  private int datasetGeneration;
  private String datasetError;

  private VideoPlaylist.Selection selection, successorSelection;
  private final java.util.List<StreamSession> retiring = new CopyOnWriteArrayList<>();
  private android.database.sqlite.SQLiteDatabase receiptIds;
  private volatile StreamSession session;
  private StreamSession preparedSession;
  private boolean preparedSessionReady;
  private AvcSource media;
  private String uid, connection, run;
  private String campaignId, scenarioId, attemptId;
  private String abortedAttemptId;
  private String startedAttemptId, preparedVideoId;
  private long controlDisconnectedNs, lastSyncNs;
  private volatile long lastControlNs = System.nanoTime();
  private JSONObject pendingFailure;
  private int inputWidth = 1280, inputHeight = 720, inputFps = 30, inputGop = 30;
  private String cellularIp = "";
  private JSONObject startRequest;
  private JSONObject successorRequest;
  // Retain video_end until the MEC acknowledges it. A cellular reattach can
  // interrupt the WebSocket after the final RTP packet but before this command.
  private JSONObject pendingEnd;
  private boolean awaitingSuccessor;
  private volatile boolean collectionMode;
  private JSONObject assignment;
  private double successorPrepareS;
  private volatile boolean enabled;
  private volatile int generation;
  private long clientSeq;
  private android.content.SharedPreferences prefs;

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    receiptIds =
        android.database.sqlite.SQLiteDatabase.openOrCreateDatabase(
            new File(getExternalFilesDir(null), "receipts.sqlite3"), null);
    receiptIds.execSQL("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)");
    prefs = getSharedPreferences("mec-v1", MODE_PRIVATE);
    clientSeq = prefs.getLong("seq", 0);
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    root.setPadding(24, 40, 24, 24);
    host = new EditText(this);
    host.setSingleLine(true);
    host.setText(prefs.getString("host", "192.168.72.136"));
    root.addView(host);
    ueAlias = new EditText(this);
    ueAlias.setSingleLine(true);
    ueAlias.setHint("Campaign UE alias (pixel1, pixel2, or pixel3)");
    ueAlias.setText(prefs.getString("ue_alias", "pixel1"));
    root.addView(ueAlias);
    application = new Spinner(this);
    application.setAdapter(
        new ArrayAdapter<String>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            new String[] {"detection", "tracking", "segmentation"}));
    root.addView(application);
    Button choose = new Button(this);
    choose.setText("Choose dataset video directory");
    choose.setOnClickListener(
        v -> {
          if (enabled || session != null || preparedSession != null) {
            show("Stop streaming before changing the dataset"); return;
          }
          Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
          i.addFlags(
              Intent.FLAG_GRANT_READ_URI_PERMISSION
                  | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
          startActivityForResult(i, 8);
        });
    root.addView(choose);
    datasetStatus = statusBox("Dataset: not selected");
    root.addView(datasetStatus);
    Button camera = new Button(this);
    camera.setText("Use rear camera");
    camera.setOnClickListener(
        v -> {
          if (enabled) { show("Stop streaming before changing the dataset"); return; }
          datasetGeneration++;
          datasetLoading = false;
          datasetError = null;
          prefs.edit().remove("dataset_uri").apply();
          event("dataset_cleared", object("reason", "camera_selected"));
          file = null;
          playlist = null;
          selection = null;
          showDataset("Dataset: rear camera");
        });
    root.addView(camera);
    Button start = new Button(this);
    start.setText("Connect and stream");
    start.setOnClickListener(
        v -> {
          if (enabled) return;
          if (datasetLoading) { show("Dataset is loading; wait before connecting"); return; }
          if (prefs.contains("dataset_uri") && playlist == null) {
            show("Dataset unavailable; reselect the dataset directory"); return;
          }
          if (file == null
              && checkSelfPermission(Manifest.permission.CAMERA)
                  != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, 9);
            return;
          }
          prefs.edit()
              .putString("host", host.getText().toString().trim())
              .putString("ue_alias", ueAlias.getText().toString().trim())
              .apply();
          enabled = true;
          showControl("Control: requesting cellular network");
          requestCellular();
        });
    root.addView(start);
    Button stop = new Button(this);
    stop.setText("End video");
    stop.setOnClickListener(v -> end());
    root.addView(stop);
    Button profile = new Button(this);
    profile.setText("Offline encoder profiling");
    profile.setOnClickListener(v -> {
      if (enabled || media != null || session != null) {
        Toast.makeText(this, "Restart the app before offline profiling; streaming is active.",
            Toast.LENGTH_LONG).show();
        return;
      }
      startActivity(new Intent(this, EncoderProfileActivity.class));
    });
    root.addView(profile);
    controlStatus = statusBox("Control: disconnected");
    root.addView(controlStatus);
    frameStatus = statusBox("Frames: transmitted=none, result=none");
    root.addView(frameStatus);
    status = statusBox("Status: idle");
    root.addView(status);
    // Aetheredge: keep controls and status reachable on smaller Pixel screens.
    ScrollView scroll = new ScrollView(this);
    scroll.setFillViewport(true);
    scroll.addView(
        root,
        new ScrollView.LayoutParams(
            ScrollView.LayoutParams.MATCH_PARENT, ScrollView.LayoutParams.WRAP_CONTENT));
    setContentView(scroll);
    timer.scheduleWithFixedDelay(() -> flushEvidence(false), 250, 250, TimeUnit.MILLISECONDS);
    String savedDataset = prefs.getString("dataset_uri", null);
    if (savedDataset != null) loadDataset(Uri.parse(savedDataset), false);
    getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    timer.scheduleAtFixedRate(() -> ui.post(() -> {
      if (!collectionMode || assignment == null || !contextReady()) return;
      long now = System.nanoTime();
      if (controlDisconnectedNs != 0 && now - controlDisconnectedNs >= 1_000_000_000L)
        failCollection("control_disconnect_timeout");
      else if (controlDisconnectedNs == 0 && now - lastControlNs >= 3_000_000_000L)
        failCollection("control_unreachable");
    }), 100, 100, TimeUnit.MILLISECONDS);
    timer.scheduleAtFixedRate(
        () -> {
          try {
            if (ws != null && (collectionMode || System.nanoTime() - lastSyncNs >= 10_000_000_000L)) {
              lastSyncNs = System.nanoTime();
              send(
                  new JSONObject()
                      .put("type", "time_sync")
                      .put("client_mono_ns", System.nanoTime()));
              if (!collectionMode) association();
            }
            if (session != null) session.deadlines();
            for (StreamSession old : retiring) old.deadlines();
          } catch (Exception e) {
            event("client_error", object("reason", e.toString()));
          }
        },
        750,
        750,
        TimeUnit.MILLISECONDS);
    timer.scheduleAtFixedRate(
        () -> {
          try {
            if (session != null) session.deadlines();
            for (StreamSession old : retiring) old.deadlines();
          } catch (Exception e) {
            show(e.toString());
          }
        },
        0,
        20,
        TimeUnit.MILLISECONDS);
  }

  private static JSONObject object(String key, Object value) {
    try {
      return new JSONObject().put(key, value);
    } catch (Exception e) {
      throw new IllegalArgumentException(e);
    }
  }

  private void show(String s) {
    ui.post(() -> status.setText("Status: " + s));
  }

  private TextView statusBox(String text) {
    TextView view = new TextView(this);
    view.setText(text);
    view.setPadding(16, 12, 16, 12);
    view.setTextColor(android.graphics.Color.BLACK);
    view.setBackgroundColor(android.graphics.Color.rgb(235, 238, 242));
    LinearLayout.LayoutParams layout =
        new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT);
    layout.setMargins(0, 8, 0, 8);
    view.setLayoutParams(layout);
    return view;
  }

  private void showDataset(String s) {
    ui.post(() -> datasetStatus.setText(s));
  }

  private void showControl(String s) {
    ui.post(() -> controlStatus.setText(s));
  }

  private final Object frameStatusLock = new Object();

  private void resetFrameStatus() {
    synchronized (frameStatusLock) {
      lastTransmittedFrame = lastResultFrame = -1;
      lastResultOutcome = "none";
      scheduleFrameRefresh();
    }
  }

  private void showTransmittedFrame(long frameId) {
    synchronized (frameStatusLock) {
      lastTransmittedFrame = Math.max(lastTransmittedFrame, frameId);
      scheduleFrameRefresh();
    }
  }

  private void showResultFrame(long frameId, String outcome) {
    synchronized (frameStatusLock) {
      if (frameId >= lastResultFrame) {
        lastResultFrame = frameId;
        lastResultOutcome = outcome;
        scheduleFrameRefresh();
      }
    }
  }

  private void scheduleFrameRefresh() {
    if (refreshPending || destroyed) return;
    refreshPending = true;
    ui.postDelayed(this::renderFrameStatus, 200);
  }

  private void renderFrameStatus() {
    String transmitted, result;
    synchronized (frameStatusLock) {
      refreshPending = false;
      transmitted = lastTransmittedFrame < 0 ? "none" : Long.toString(lastTransmittedFrame);
      result = lastResultFrame < 0 ? "none" : lastResultFrame + " (" + lastResultOutcome + ")";
    }
    if (!destroyed) frameStatus.setText("Frames: transmitted=" + transmitted + ", result=" + result);
  }

  // Persist lifecycle sequence numbers so retries/reconnects do not reuse command IDs.
  private synchronized long seq() {
    clientSeq++;
    prefs.edit().putLong("seq", clientSeq).commit();
    return clientSeq;
  }

  // Only this executor touches the writer. A queued flush follows all prior writes.
  private void flushEvidence(boolean close) {
    log.execute(() -> {
      try {
        if (evidenceWriter == null) return;
        if (close) {
          evidenceWriter.close();
          evidenceWriter = null;
        } else evidenceWriter.flush();
      } catch (IOException e) {
        enabled = false;
        show("Evidence flush failed: " + e);
      }
    });
  }

  // Copy the envelope without modifying the caller's event or reparsing JSON.
  private void event(String type, JSONObject value) {
    try {
      JSONObject record = new JSONObject();
      java.util.Iterator<String> keys = value.keys();
      while (keys.hasNext()) {
        String key = keys.next();
        record.put(key, value.get(key));
      }
      record.put("type", type)
              .put("run_id", run)
              .put("ue_id", uid)
              .put("monotonic_ns", System.nanoTime());
      if (!record.has("campaign_id"))
        record.put("campaign_id", campaignId == null ? JSONObject.NULL : campaignId);
      if (!record.has("scenario_id"))
        record.put("scenario_id", scenarioId == null ? JSONObject.NULL : scenarioId);
      if (!record.has("attempt_id"))
        record.put("attempt_id", attemptId == null ? JSONObject.NULL : attemptId);
      // Serialize once while the producer owns the values. Queue an immutable line.
      String line = record.toString();
      log.execute(
          () -> {
            try {
              if (evidenceWriter == null)
                evidenceWriter = new BufferedWriter(new FileWriter(
                    new File(getExternalFilesDir(null), "events.jsonl"), true));
              evidenceWriter.write(line);
              evidenceWriter.newLine();
            } catch (Exception e) {
              enabled = false;
              show("Evidence write failed: " + e);
            }
          });
      if (type.equals("media_end") && session != null)
        ui.post(() -> mediaEnded(value));
      if (type.equals("media_prepared")) ui.post(() -> mediaPrepared(value));
      if (type.equals("transmitted")) showTransmittedFrame(value.getLong("frame_id"));
      if (type.equals("successor_needed") && playlist != null && !collectionMode)
        ui.post(() -> prepareSuccessor(value));
      if (type.equals("plan_unapplied")) send(new JSONObject(value.toString()).put("type", type));
      if (type.equals("sender_error")) {
        if (collectionMode) ui.post(() -> failCollection("media_sender_failed"));
        show(value.toString());
        // Aetheredge: close a failed collection input so the MEC can resolve
        // its received tail and the campaign runner can retry the scenario.
        if (collectionMode && session != null && session.lastFrame() >= 0) {
          org.json.JSONObject end =
              new org.json.JSONObject()
                  .put("video_id", session.video())
                  .put("final_frame_id", session.lastFrame());
          ui.post(() -> mediaEnded(end));
        }
      }
    } catch (Exception e) {
      enabled = false;
      show(e.toString());
    }
  }

  private void requestCellular() {
    connectivity = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
    callback =
        new ConnectivityManager.NetworkCallback() {
          public void onAvailable(Network n) {
            Log.i(TAG, "Cellular network available: " + n);
            ui.post(
                () -> {
                  network = n;
                  rebindMedia(n);
                  connect();
                });
          }

          public void onLinkPropertiesChanged(Network n, LinkProperties p) {
            String next = "";
            for (LinkAddress a : p.getLinkAddresses())
              if (a.getAddress() instanceof Inet4Address) {
                next = a.getAddress().getHostAddress();
                break;
              }
            final String ip = next;
            ui.post(
                () -> {
                  if (n.equals(network) && !ip.isEmpty()) {
                    boolean changed = !ip.equals(cellularIp);
                    if (changed && !cellularIp.isEmpty()) failCollection("control_ip_changed");
                    cellularIp = ip;
                    if (changed) {
                      Log.i(TAG, "Cellular IPv4 address ready: " + ip);
                      rebindMedia(n);
                      connect();
                    }
                  }
                });
          }

          public void onLost(Network n) {
            Log.w(TAG, "Cellular network lost: " + n);
            ui.post(
                () -> {
                  if (n.equals(network)) {
                    if (controlDisconnectedNs == 0) controlDisconnectedNs = System.nanoTime();
                    network = null;
                    if (ws != null) ws.cancel();
                    ws = null;
                    resetFrameStatus();
                    showControl("Control: cellular lost; waiting for automatic recovery");
                  }
                });
          }

          public void onUnavailable() {
            Log.e(TAG, "No cellular network satisfied the request");
            resetFrameStatus();
            showControl("Control: cellular network unavailable");
          }
        };
    connectivity.requestNetwork(
        new NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build(),
        callback);
  }

  private void connect() {
    if (!enabled || network == null) return;
    final int g = ++generation;
    String address = prefs.getString("host", "");
    Log.i(TAG, "Opening MEC control connection to " + address + " from " + cellularIp);
    showControl("Control: connecting to MEC " + address);
    OkHttpClient client =
        new OkHttpClient.Builder()
            .socketFactory(network.getSocketFactory())
            .dns(name -> java.util.Arrays.asList(network.getAllByName(name)))
            .proxy(java.net.Proxy.NO_PROXY)
            // Four concurrent result streams can delay a pong on the 51-PRB
            // data path without breaking the socket.  Allow that transient
            // backpressure to clear before OkHttp tears down healthy control.
            .pingInterval(30, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)
            .build();
    Request.Builder request = new Request.Builder().url("ws://" + address + ":8765/ws/ue");
    String token = prefs.getString("token:" + address, null);
    if (token != null) request.header("x-mec-resume-token", token);
    if (ws != null) ws.cancel();
    ws =
        client.newWebSocket(
            request.build(),
            new WebSocketListener() {
              public void onOpen(WebSocket socket, Response response) {
                Log.i(TAG, "MEC control connection open");
                showControl("Control: MEC connected");
              }

              public void onMessage(WebSocket socket, String raw) {
                long receipt = System.nanoTime();
                if (destroyed || g != generation) return;
                lastControlNs = receipt;
                try {
                  int queuedAhead = incoming.getQueue().size();
                  incoming.execute(() -> dispatch(raw, receipt, g, queuedAhead));
                } catch (RejectedExecutionException e) {
                  if (!destroyed) throw e;
                }
              }

              public void onFailure(WebSocket socket, Throwable e, Response r) {
                Log.e(TAG, "MEC control connection failed", e);
                if (g != generation) return;
                resetFrameStatus();
                showControl("Control: MEC failed: " + e.getMessage());
                ui.post(() -> {
                  if (g != generation) return;
                  if (controlDisconnectedNs == 0) controlDisconnectedNs = System.nanoTime();
                  retry(g);
                });
              }

              public void onClosed(WebSocket socket, int c, String reason) {
                Log.w(TAG, "MEC control connection closed: " + c + " " + reason);
                if (g != generation) return;
                resetFrameStatus();
                ui.post(() -> {
                  if (g != generation) return;
                  if (controlDisconnectedNs == 0) controlDisconnectedNs = System.nanoTime();
                  retry(g);
                });
              }
            });
  }

  // Socket creation and connect may perform network I/O; keep both off Android's UI thread.
  private void rebindMedia(Network n) {
    StreamSession active = session;
    StreamSession prepared = preparedSession;
    if (active == null && prepared == null) return;
    networkIo.execute(
        () -> {
          try {
            if (active != null) active.network(n);
            if (prepared != null) prepared.network(n);
          } catch (Exception e) {
            event("media_rebind_error", object("reason", e.toString()));
          }
        });
  }

  // Start media only after its cellular-bound UDP socket is ready.
  private void startAdmittedSession(StreamSession admitted, Uri source) {
    Network n = network;
    networkIo.execute(
        () -> {
          try {
            admitted.network(n);
            ui.post(
                () -> {
                  if (session != admitted) return;
                  try {
                    media =
                        new AvcSource(
                            this,
                            admitted,
                            this::event,
                            playlist == null || collectionMode ? 0 : successorPrepareS);
                    if (collectionMode && assignment != null && playlist != null && playlist.isEncoded())
                      media.startEncoded(source, selection.name, assignment);
                    else if (collectionMode && assignment != null)
                      media.start(
                          source,
                          assignment.getInt("source_start_frame"),
                          assignment.getInt("frame_count"));
                    else media.start(source);
                    show("Streaming " + admitted.video());
                  } catch (Exception e) {
                    event("sender_error", object("reason", e.toString()));
                  }
                });
          } catch (Exception e) {
            event("media_setup_error", object("reason", e.toString()));
          }
        });
  }

  // Prepare a successor's socket without interrupting the active video's socket.
  private void prepareAdmittedSession(StreamSession prepared) {
    Network n = network;
    networkIo.execute(
        () -> {
          try {
            prepared.network(n);
            ui.post(
                () -> {
                  if (preparedSession != prepared) return;
                  preparedSessionReady = true;
                  if (awaitingSuccessor) {
                    try {
                      startPreparedSuccessor();
                    } catch (Exception e) {
                      event("successor_start_error", object("reason", e.toString()));
                    }
                  }
                });
          } catch (Exception e) {
            event("media_setup_error", object("reason", e.toString()));
          }
        });
  }

  private void retry(int g) {
    ui.postDelayed(
        () -> {
          if (g == generation && enabled) {
            ws = null;
            showControl("Control: reconnecting");
            connect();
          }
        },
        500);
  }

  private void timeSync() {
    try {
      send(new JSONObject().put("type", "time_sync").put("client_mono_ns", System.nanoTime()));
    } catch (Exception e) {
      show(e.toString());
    }
  }

  private void send(JSONObject m) {
    try {
      // Aetheredge: every UE message carries the immutable scenario join key.
      m.put("campaign_id", campaignId == null ? JSONObject.NULL : campaignId)
          .put("scenario_id", scenarioId == null ? JSONObject.NULL : scenarioId)
          .put("attempt_id", attemptId == null ? JSONObject.NULL : attemptId);
      WebSocket s = ws;
      if (s != null && !s.send(m.toString())) show("Control send queue unavailable");
    } catch (JSONException e) {
      show("Scenario context encoding failed: " + e);
    }
  }

  private void setContext(JSONObject m) throws JSONException {
    boolean campaign = m.has("campaign_id") && !m.isNull("campaign_id");
    boolean scenario = m.has("scenario_id") && !m.isNull("scenario_id");
    boolean attempt = m.has("attempt_id") && !m.isNull("attempt_id");
    if ((campaign || scenario || attempt) && !(campaign && scenario && attempt))
      throw new JSONException("incomplete scenario context");
    campaignId = campaign ? m.getString("campaign_id") : null;
    scenarioId = scenario ? m.getString("scenario_id") : null;
    attemptId = attempt ? m.getString("attempt_id") : null;
  }

  private boolean contextReady() {
    return campaignId != null && scenarioId != null && attemptId != null;
  }

  private void association() throws Exception {
    Network n = network;
    if (n == null || uid == null) return;
    LinkProperties p = connectivity.getLinkProperties(n);
    if (p == null) return;
    for (LinkAddress a : p.getLinkAddresses())
      if (a.getAddress() instanceof Inet4Address) {
        send(
            new JSONObject()
                .put("type", "association_update")
                .put("ue_alias", prefs.getString("ue_alias", "pixel1"))
                .put("ue_ip", a.getAddress().getHostAddress())
                .put("clock_uncertainty_ns", clock.valid() ? clock.uncertainty() : JSONObject.NULL)
                .put(
                    "clock_mapping",
                    new JSONObject()
                        .put("offset_ns", clock.offset())
                        .put("method", "four_timestamp_min_RTT")));
        return;
      }
  }

  // Record preparation decisions without changing admission or retry behavior.
  private void preparationTrace(String stage, String reason) {
    try {
      event("collection_preparation", new JSONObject()
          .put("stage", stage)
          .put("reason", reason == null ? JSONObject.NULL : reason)
          .put("enabled", enabled)
          .put("collection_mode", collectionMode)
          .put("dataset_loading", datasetLoading)
          .put("dataset_error", datasetError == null ? JSONObject.NULL : datasetError)
          .put("playlist_present", playlist != null)
          .put("assignment_present", assignment != null)
          .put("session_present", session != null)
          .put("prepared_session_present", preparedSession != null)
          .put("start_request_present", startRequest != null)
          .put("attempt_present", attemptId != null)
          .put("attempt_aborted", attemptId != null && attemptId.equals(abortedAttemptId))
          .put("media_path", assignment == null ? JSONObject.NULL
              : assignment.opt("media_path")));
    } catch (Exception e) {
      Log.w(TAG, "Could not record preparation diagnostic", e);
    }
  }

  private synchronized void requestStart() throws Exception {
    preparationTrace("request_start_enter", null);
    if (collectionMode && (assignment == null || attemptId == null
        || attemptId.equals(abortedAttemptId))) {
      preparationTrace("request_start_skipped", assignment == null ? "assignment_missing"
          : attemptId == null ? "attempt_missing" : "attempt_aborted");
      return;
    }
    if (!enabled || session != null || startRequest != null) {
      preparationTrace("request_start_skipped", !enabled ? "client_disabled"
          : session != null ? "session_present" : "start_request_present");
      return;
    }
    if (playlist != null && playlist.isEncoded() && !collectionMode)
      throw new IllegalStateException("Encoded assets require campaign collection mode");
    startRequest =
        new JSONObject()
            .put("type", "video_start")
            .put("ue_id", uid)
            .put("client_seq", seq())
            .put(
                "application",
                assignment == null
                    ? application.getSelectedItem().toString()
                    : assignment.getString("application"))
            .put("slo_ms", 150)
            .put("can_drop", false)
            .put("can_drop_frames", false)
            .put(
                "input",
                new JSONObject()
                    .put("width", inputWidth)
                    .put("height", inputHeight)
                    .put("fps", inputFps));
    if (assignment != null)
      for (String key :
          new String[] {"candidate_id", "media_id", "source_start_frame", "frame_count"})
        startRequest.put(key, assignment.get(key));
    preparationTrace("video_start_send_called", null);
    send(startRequest);
  }

  private void mediaPrepared(JSONObject value) {
    try {
      if (!collectionMode || session == null || !contextReady()
          || attemptId.equals(abortedAttemptId)
          || !session.video().equals(value.getString("video_id"))) return;
      preparedVideoId = session.video();
      send(new JSONObject().put("type", "scenario_context_ack").put("video_id", preparedVideoId));
    } catch (Exception e) {
      event("scenario_prepare_error", object("reason", e.toString()));
    }
  }

  private void failCollection(String reason) {
    if (!collectionMode || !contextReady() || attemptId.equals(abortedAttemptId)) return;
    try {
      pendingFailure = new JSONObject().put("type", "scenario_failure").put("reason", reason)
          .put("campaign_id", campaignId).put("scenario_id", scenarioId).put("attempt_id", attemptId);
      abortCollection(pendingFailure);
      send(pendingFailure);
    } catch (Exception e) { show(e.toString()); }
  }

  private void abortCollection(JSONObject m) throws Exception {
    if (!collectionMode || !contextReady()
        || !campaignId.equals(m.optString("campaign_id"))
        || !scenarioId.equals(m.optString("scenario_id"))
        || !attemptId.equals(m.optString("attempt_id"))) return;
    cpu.stop();
    abortedAttemptId = attemptId;
    // Set the packet-level flag before asynchronous codec cleanup.
    if (session != null) session.stopTransmission();
    if (media != null) media.close();
    if (session != null) session.close();
    if (preparedSession != null) preparedSession.close();
    media = null;
    session = null;
    preparedSession = null;
    assignment = null;
    preparedVideoId = null;
    startRequest = null;
    successorRequest = null;
    pendingEnd = null;
    event("scenario_aborted", object("error", m.opt("error")));
    flushEvidence(false);
  }

  // Controls still run on the UI thread. Wait only on the background worker so
  // a later result observes the session established by the preceding control.
  private void dispatch(String raw, long receipt, int g, int queuedAhead) {
    long start = System.nanoTime();
    if (g != generation) return;
    JSONObject timing = object("receipt_mono_ns", receipt);
    try {
      timing.put("dispatch_start_mono_ns", start)
          .put("worker_queue_ns", start - receipt)
          .put("queued_ahead", queuedAhead)
          .put("connection_generation", g);
      long parseStart = System.nanoTime();
      JSONObject result = resultEnvelope(raw);
      if (result != null) {
        long parsed = System.nanoTime();
        identifyMessage(timing, result);
        timing.put("parse_ns", parsed - parseStart);
        handleResult(result, receipt, timing);
      } else {
        JSONObject control = new JSONObject(raw);
        long parsed = System.nanoTime();
        identifyMessage(timing, control);
        timing.put("parse_ns", parsed - parseStart);
        if (collectionMode && "association_status".equals(control.optString("type"))) {
          timing.put("ui_skipped", true);
          return;
        }
        long[] uiTimes = new long[2];
        FutureTask<Void> applied = new FutureTask<>(() -> {
          uiTimes[0] = System.nanoTime();
          try {
            if (!destroyed && g == generation) message(control, receipt);
          } finally {
            uiTimes[1] = System.nanoTime();
          }
          return null;
        });
        long posted = System.nanoTime();
        ui.post(applied);
        try {
          applied.get();
        } finally {
          long resumed = System.nanoTime();
          timing.put("ui_wait_ns", resumed - posted);
          if (applied.isDone() && uiTimes[1] != 0) {
            timing.put("ui_queue_ns", uiTimes[0] - posted)
                .put("ui_handler_ns", uiTimes[1] - uiTimes[0]);
          }
        }
      }
    } catch (Exception e) {
      event("protocol_error", object("reason", e.toString()));
      show(e.toString());
    } finally {
      event("incoming_message_timing", timing);
    }
  }

  private static void identifyMessage(JSONObject timing, JSONObject message) throws JSONException {
    timing.put("message_type", message.optString("type"));
    for (String key : new String[] {"campaign_id", "scenario_id", "attempt_id",
                                    "video_id", "frame_id", "result_id"}) {
      if (message.has(key)) timing.put(key, message.get(key));
    }
  }

  // Read just the result fields consumed by the app, in any JSON key order.
  static JSONObject resultEnvelope(String raw) throws Exception {
    JSONObject result = new JSONObject();
    try (android.util.JsonReader reader = new android.util.JsonReader(new StringReader(raw))) {
      reader.beginObject();
      while (reader.hasNext()) {
        String key = reader.nextName();
        switch (key) {
          case "type":
            if (!"frame_result".equals(reader.nextString())) return null;
            result.put("type", "frame_result");
            break;
          case "frame_id":
          case "video_id": case "result_id": case "outcome": case "reason":
          case "campaign_id": case "scenario_id": case "attempt_id":
            if (reader.peek() == android.util.JsonToken.NULL) {
              reader.nextNull();
              result.put(key, JSONObject.NULL);
            } else if (reader.peek() == android.util.JsonToken.STRING
                || reader.peek() == android.util.JsonToken.NUMBER) {
              result.put(key, reader.nextString());
            } else reader.skipValue();
            break;
          case "payload":
            // Older replay messages may carry receipt fields after the payload.
            if (result.has("type") && result.has("frame_id") && result.has("video_id")
                && result.has("result_id") && result.has("outcome") && result.has("reason")
                && result.has("campaign_id") && result.has("scenario_id") && result.has("attempt_id"))
              return result;
            reader.skipValue();
            break;
          default: reader.skipValue();
        }
      }
      reader.endObject();
      return result.has("type") ? result : null;
    }
  }

  private void handleResult(JSONObject m, long receipt, JSONObject timing) throws Exception {
    String id = m.getString("result_id");
    boolean duplicate;
    long queryStart = System.nanoTime();
    try (android.database.Cursor cursor =
        receiptIds.rawQuery("SELECT 1 FROM seen WHERE id=?", new String[] {id})) {
      duplicate = cursor.moveToFirst();
    }
    timing.put("sqlite_select_ns", System.nanoTime() - queryStart)
        .put("duplicate", duplicate);
    if (duplicate) return;
    long insertStart = System.nanoTime();
    receiptIds.execSQL("INSERT INTO seen VALUES (?)", new Object[] {id});
    timing.put("sqlite_insert_ns", System.nanoTime() - insertStart);
    StreamSession owner = session;
    if (owner != null && !owner.video().equals(m.getString("video_id"))) owner = null;
    for (StreamSession old : retiring)
      if (old.video().equals(m.getString("video_id"))) owner = old;
    if (owner != null) owner.receipt(m, receipt);
    else
      event(
          "result_receipt",
          new JSONObject()
              .put("campaign_id", m.opt("campaign_id"))
              .put("scenario_id", m.opt("scenario_id"))
              .put("attempt_id", m.opt("attempt_id"))
              .put("video_id", m.getString("video_id"))
              .put("frame_id", m.getLong("frame_id"))
              .put("result_id", id)
              .put("receipt_mono_ns", receipt)
              .put("outcome", m.getString("outcome"))
              .put("reason", m.isNull("reason") ? JSONObject.NULL : m.getString("reason"))
              .put("latency_ns", JSONObject.NULL)
              .put("slo_success", false));
    showResultFrame(m.getLong("frame_id"), m.getString("outcome"));
  }

  private void message(JSONObject m, long receipt) throws Exception {
    String type = m.getString("type");
    switch (type) {
      case "mec_hello":
        if (controlDisconnectedNs != 0 && System.nanoTime() - controlDisconnectedNs >= 1_000_000_000L)
          failCollection("control_disconnect_timeout");
        controlDisconnectedNs = 0;
        if (session != null
            && (!m.optString("active_video_id").equals(session.video())
                || run != null && !run.equals(m.getString("run_id")))) {
          if (media != null) media.close();
          if (session != null) session.close();
          cpu.stop();
          flushEvidence(false);
          session = null;
          media = null;
          startRequest = null;
          pendingEnd = null;
        }
        run = m.getString("run_id");
        uid = m.getString("ue_id");
        setContext(m);
        if (pendingFailure != null) {
          if (pendingFailure.optString("attempt_id").equals(attemptId)) send(pendingFailure);
          else pendingFailure = null;
        }
        connection = m.getString("connection_id");
        // Use the MEC's validated input profile for configurable FPS experiments.
        JSONObject inputProfile = m.optJSONObject("input_profile");
        if (inputProfile != null) {
          inputWidth = inputProfile.getInt("width");
          inputHeight = inputProfile.getInt("height");
          inputFps = inputProfile.getInt("fps");
          inputGop = inputProfile.getInt("gop_frames");
        }
        successorPrepareS = m.optDouble("successor_prepare_s", 5.0);
        collectionMode = m.optBoolean("collection_mode", false);
        prefs
            .edit()
            .putString("token:" + prefs.getString("host", ""), m.getString("resume_token"))
            .commit();
        for (int i = 0; i < 4; i++) {
          final int wait = i * 50;
          ui.postDelayed(() -> timeSync(), wait);
        }
        association();
        // Lifecycle commands are idempotent by client_seq on the MEC. Reusing
        // the original command closes a video whose first video_end was lost.
        if (pendingEnd != null) send(pendingEnd);
        break;
      case "scenario_stop":
        if (collectionMode && contextReady()
            && campaignId.equals(m.optString("campaign_id"))
            && scenarioId.equals(m.optString("scenario_id"))
            && attemptId.equals(m.optString("attempt_id"))) {
          if (media != null) media.stopTransmission();
          if (pendingEnd != null) send(pendingEnd);
        }
        break;
      case "scenario_abort":
        abortCollection(m);
        break;
      case "scenario_context":
        if (m.optString("attempt_id").equals(abortedAttemptId)) break;
        if (m.optString("attempt_id").equals(attemptId) && session != null) {
          if (session.video().equals(preparedVideoId))
            mediaPrepared(new JSONObject().put("video_id", preparedVideoId));
          break;
        }
        if (m.optString("attempt_id").equals(attemptId) && startRequest != null) {
          send(startRequest);
          break;
        }
        if (session != null || preparedSession != null)
          throw new IllegalStateException("scenario context changed during active media");
        setContext(m);
        assignment = m.optBoolean("active", false) ? m.optJSONObject("assignment") : null;
        preparedVideoId = null;
        startRequest = null;
        successorRequest = null;
        resetFrameStatus();
        event("scenario_context", new JSONObject());
        if (assignment != null) prepareDatasetAssignment();
        break;
      case "scenario_start":
        if (!contextReady() || !campaignId.equals(m.optString("campaign_id"))
            || !scenarioId.equals(m.optString("scenario_id"))
            || !attemptId.equals(m.optString("attempt_id"))
            || attemptId.equals(abortedAttemptId) || attemptId.equals(startedAttemptId)
            || session == null || media == null || !session.video().equals(preparedVideoId)) break;
        startedAttemptId = attemptId;
        cpu.start(campaignId, scenarioId, attemptId);
        media.release();
        event("scenario_start", object("release_monotonic_ns", System.nanoTime()));
        break;
      case "time_sync":
        clock.observe(
            m.getLong("client_mono_ns"),
            m.getLong("server_receive_unix_ns"),
            m.getLong("server_send_unix_ns"),
            receipt);
        association();
        break;
      case "association_probe":
        probe(m);
        break;
      case "association_status":
        if (collectionMode) break;
        if (m.getBoolean("valid") && successorRequest != null) send(successorRequest);
        if (m.getBoolean("valid")) requestStart();
        break;
      case "video_response":
        if (collectionMode && (!contextReady()
            || !attemptId.equals(m.optString("attempt_id"))
            || attemptId.equals(abortedAttemptId))) break;
        if (successorRequest != null
            && m.getLong("client_seq") == successorRequest.getLong("client_seq")) {
          if (m.getBoolean("accepted")) {
            event(
                "admission_decision",
                new JSONObject()
                    .put("video_id", m.getString("video_id"))
                    .put("accepted", true)
                    .put("reason", JSONObject.NULL));
            preparedSession = new StreamSession(m, clock, this::event, getExternalFilesDir(null));
            preparedSessionReady = false;
            prepareAdmittedSession(preparedSession);
          } else {
            event(
                "admission_decision",
                new JSONObject()
                    .put("video_id", m.optString("video_id"))
                    .put("accepted", false)
                    .put("reason", m.optString("reason")));
            event(
                "successor_not_prepared",
                new JSONObject().put("reason", m.optString("reason")));
            successorRequest = null;
          }
        } else if (m.getBoolean("accepted") && session == null) {
          event(
              "admission_decision",
              new JSONObject()
                  .put("campaign_id", m.opt("campaign_id"))
                  .put("scenario_id", m.opt("scenario_id"))
                  .put("attempt_id", m.opt("attempt_id"))
                  .put("video_id", m.getString("video_id"))
                  .put("accepted", true)
                  .put("reason", JSONObject.NULL));
          session =
              new StreamSession(
                  m,
                  clock,
                  this::event,
                  getExternalFilesDir(null));
          startAdmittedSession(session, file);
        } else if (!m.getBoolean("accepted")) {
          event(
              "admission_decision",
              new JSONObject()
                  .put("video_id", m.optString("video_id"))
                  .put("accepted", false)
                  .put("reason", m.optString("reason")));
          show("Admission: " + m.optString("reason"));
          startRequest = null;
        }
        break;
      // Route plans only to their video; retired sessions remain available for late receipts.
      case "gop_config":
        if (session != null && session.video().equals(m.getString("video_id"))) session.command(m);
        else if (preparedSession != null
            && preparedSession.video().equals(m.getString("video_id")))
          preparedSession.command(m);
        break;
      case "video_status":
        if (session != null && session.video().equals(m.getString("video_id"))) {
          if (m.getString("status").equals("SUSPENDED"))
            session.suspend(m);
          else if (java.util.Arrays.asList("COMPLETED", "FAILED", "EXPIRED", "CANCELLED")
              .contains(m.getString("status"))) {
            if (pendingEnd != null
                && pendingEnd.optString("video_id").equals(m.getString("video_id")))
              pendingEnd = null;
            show(m.toString());
            if (media != null) media.close();
            retire(session);
            session = null;
            startRequest = null;
            // Aetheredge: a collection assignment is single-use. The next
            // attempt supplies a new assignment through scenario_context.
            if (collectionMode) {
              cpu.stop();
              flushEvidence(false);
              assignment = null;
            }
            if (enabled && !collectionMode && playlist != null && preparedSession != null) {
              startPreparedSuccessor();
            } else if (enabled && !collectionMode && playlist != null) {
              // A failed early preparation retries through ordinary admission.
              if (successorSelection == null) {
                successorSelection = playlist.next();
                recordSelection(successorSelection);
              }
              selection = successorSelection;
              file = selection.uri;
              successorSelection = null;
            } else if ((!enabled || playlist == null) && !m.getString("status").equals("EXPIRED")) {
              enabled = false;
              ui.postDelayed(
                  () -> {
                    if (!enabled && ws != null) ws.close(1000, "reconciled");
                  },
                  5200);
            }
          }
        } else if (preparedSession != null
            && preparedSession.video().equals(m.getString("video_id"))
            && java.util.Arrays.asList("FAILED", "EXPIRED", "CANCELLED")
                .contains(m.getString("status"))) {
          preparedSession.close();
          preparedSession = null;
          preparedSessionReady = false;
          successorRequest = null;
          event("successor_not_prepared", object("reason", m.getString("status")));
        }
        break;
      case "video_end_ack":
        if (pendingEnd != null
            && pendingEnd.optLong("client_seq", -1) == m.optLong("client_seq", -2))
          pendingEnd = null;
        break;
      case "error":
        show(m.toString());
        event("server_error", m);
        break;
    }
  }

  private void retire(StreamSession old) {
    old.close();
    retiring.add(old);
    ui.postDelayed(() -> retiring.remove(old), 5200);
  }

  // Request the next prerecorded session while the current file still has lead time.
  private void prepareSuccessor(JSONObject value) {
    try {
      if (session == null
          || successorRequest != null
          || preparedSession != null
          || !session.video().equals(value.getString("video_id"))) return;
      successorSelection = playlist.next();
      recordSelection(successorSelection);
      successorRequest =
          new JSONObject()
              .put("type", "video_start")
              .put("ue_id", uid)
              .put("client_seq", seq())
              .put("predecessor_video_id", session.video())
              .put("expected_start_unix_ns", value.getLong("expected_start_unix_ns"))
              .put("application", application.getSelectedItem().toString())
              .put("slo_ms", 150)
              .put("can_drop", false)
              .put("can_drop_frames", false)
              .put(
                  "input",
                  new JSONObject()
                      .put("width", inputWidth)
                      .put("height", inputHeight)
                      .put("fps", inputFps));
      send(successorRequest);
    } catch (Exception e) {
      event("successor_prepare_error", object("reason", e.toString()));
    }
  }

  // Queue video_end, then switch locally without waiting for its acknowledgement.
  private void mediaEnded(JSONObject value) {
    try {
      if (session == null || !session.video().equals(value.getString("video_id"))) return;
      if (pendingEnd == null
          || !pendingEnd.optString("video_id").equals(session.video()))
        pendingEnd =
            new JSONObject()
              .put("type", "video_end")
              .put("ue_id", uid)
              .put("video_id", session.video())
              .put("client_seq", seq())
              .put("final_frame_id", value.getLong("final_frame_id"))
              .put("frames_fully_sent", value.opt("frames_fully_sent"))
              .put("end_reason", value.opt("end_reason"));
      send(pendingEnd);
      awaitingSuccessor = enabled && !collectionMode && playlist != null;
      if (enabled && preparedSession != null && preparedSessionReady) startPreparedSuccessor();
      else if (playlist != null)
        event("successor_response_pending", object("reason", "response_pending_at_eof"));
    } catch (Exception e) {
      event("client_error", object("reason", e.toString()));
    }
  }

  private void startPreparedSuccessor() throws Exception {
    if (preparedSession == null || !preparedSessionReady || successorSelection == null) return;
    if (session != null) retire(session);
    session = preparedSession;
    selection = successorSelection;
    file = selection.uri;
    preparedSession = null;
    preparedSessionReady = false;
    successorSelection = null;
    successorRequest = null;
    awaitingSuccessor = false;
    media = new AvcSource(this, session, this::event, successorPrepareS);
    media.start(file);
    event("successor_started", object("video_id", session.video()));
    show("Streaming " + selection.name);
  }

  private void recordSelection(VideoPlaylist.Selection selected) {
    try {
      event(
          "video_selected",
          new JSONObject()
              .put("source", selected.name)
              .put("playlist_cycle", selected.cycle)
              .put("playlist_position", selected.position)
              .put("shuffle_seed", playlist.seed));
    } catch (Exception e) {
      throw new IllegalArgumentException(e);
    }
  }

  // Enumerate videos through Android's persisted document-tree permission.
  private void collectVideos(Uri tree, String documentId, java.util.List<VideoPlaylist.Selection> out)
      throws Exception {
    Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, documentId);
    String[] projection = {
      DocumentsContract.Document.COLUMN_DOCUMENT_ID,
      DocumentsContract.Document.COLUMN_DISPLAY_NAME,
      DocumentsContract.Document.COLUMN_MIME_TYPE
    };
    try (android.database.Cursor cursor =
        getContentResolver().query(children, projection, null, null, null)) {
      if (cursor == null) return;
      while (cursor.moveToNext()) {
        String id = cursor.getString(0), name = cursor.getString(1), mime = cursor.getString(2);
        if (DocumentsContract.Document.MIME_TYPE_DIR.equals(mime)) {
          collectVideos(tree, id, out);
        } else if (name.endsWith(".pav") || (mime != null && mime.startsWith("video/"))
            || name.toLowerCase(java.util.Locale.ROOT).matches(".*\\.(mp4|mov|mkv|m4v)$")) {
          out.add(
              new VideoPlaylist.Selection(
                  DocumentsContract.buildDocumentUriUsingTree(tree, id), name, 0, 0));
        }
      }
    }
  }

  private void probe(JSONObject m) {
    final Network n = network;
    final String probeUid = uid, probeConnection = connection;
    probes.execute(
        () -> {
          try (DatagramSocket socket = new DatagramSocket()) {
            if (n == null) return;
            n.bindSocket(socket);
            JSONObject endpoint = m.getJSONObject("endpoint");
            socket.connect(
                InetAddress.getByName(endpoint.getString("host")), endpoint.getInt("port"));
            JSONArray pattern = m.getJSONArray("pattern");
            long start = m.getLong("start_unix_ns"),
                interval = (long) (m.getDouble("interval_s") * 1e9);
            for (int i = 0; i < pattern.length(); i++) {
              long begin = start + i * interval, end = begin + interval;
              while (clock.now() < begin)
                TimeUnit.NANOSECONDS.sleep(Math.min(10_000_000L, begin - clock.now()));
              byte[] packet = new byte[1000];
              byte[] h =
                  ("MECP1 "
                          + probeUid
                          + " "
                          + probeConnection
                          + " "
                          + m.getString("probe_id")
                          + " "
                          + i
                          + "\n")
                      .getBytes(java.nio.charset.StandardCharsets.US_ASCII);
              System.arraycopy(h, 0, packet, 0, h.length);
              long next = clock.now();
              while (pattern.getInt(i) == 1 && clock.now() < end) {
                socket.send(new DatagramPacket(packet, packet.length));
                next += (long) (packet.length * 8e9 / m.getDouble("rate_bps"));
                long delay = next - clock.now();
                if (delay > 0) TimeUnit.NANOSECONDS.sleep(delay);
              }
            }
            event("association_probe", object("probe_id", m.getString("probe_id")));
          } catch (Exception e) {
            event("probe_error", object("reason", e.toString()));
          }
        });
  }

  private void end() {
    try {
      // Stop media without forgetting the selected dataset.
      enabled = false;
      awaitingSuccessor = false;
      if (preparedSession != null) preparedSession.close();
      preparedSession = null;
      preparedSessionReady = false;
      if (media != null) media.close();
      if (session != null && session.lastFrame() >= 0) {
        pendingEnd =
            new JSONObject()
                .put("type", "video_end")
                .put("ue_id", uid)
                .put("video_id", session.video())
                .put("client_seq", seq())
                .put("final_frame_id", session.lastFrame());
        send(pendingEnd);
      } else {
        enabled = false;
        if (ws != null) ws.close(1000, "stopped");
      }
    } catch (Exception e) {
      show(e.toString());
    }
  }

  // Dataset state and assignment lookup are owned by the UI thread. Only
  // directory enumeration runs in the worker; generation fences discard stale loads.
  private void loadDataset(Uri tree, boolean selected) {
    final int token = ++datasetGeneration;
    datasetLoading = true;
    datasetError = null;
    playlist = null;
    file = null;
    selection = null;
    showDataset("Loading dataset...");
    event("dataset_load_started", object("reason", selected ? "selection" : "restore"));
    datasetIo.execute(() -> {
      VideoPlaylist loaded = null;
      String error = null;
      try {
        java.util.List<VideoPlaylist.Selection> files = new java.util.ArrayList<>();
        collectVideos(tree, DocumentsContract.getTreeDocumentId(tree), files);
        loaded = new VideoPlaylist(files);
      } catch (Exception e) { error = e.toString(); }
      final VideoPlaylist result = loaded;
      final String failure = error;
      ui.post(() -> {
        if (destroyed || token != datasetGeneration) return;
        datasetLoading = false;
        datasetError = failure;
        if (result == null) {
          event("dataset_load_failed", object("reason", failure));
          showDataset("Dataset unavailable; reselect directory: " + failure);
        } else {
          playlist = result;
          selection = playlist.next();
          file = selection.uri;
          prefs.edit().putString("dataset_uri", tree.toString()).apply();
          event(selected ? "dataset_selected" : "dataset_restored", object("videos", playlist.size()));
          event("playlist_initialized", object("videos", playlist.size()));
          recordSelection(selection);
          showDataset("Dataset directory: " + playlist.size()
              + (playlist.isEncoded() ? " pre-encoded assets" : " videos"));
        }
        if (assignment != null) prepareDatasetAssignment();
      });
    });
  }

  private void prepareDatasetAssignment() {
    preparationTrace("dataset_assignment_enter", null);
    if (datasetLoading || assignment == null || attemptId.equals(abortedAttemptId)) {
      preparationTrace("dataset_assignment_deferred", datasetLoading ? "dataset_loading"
          : assignment == null ? "assignment_missing" : "attempt_aborted");
      return;
    }
    try {
      if (playlist == null)
        throw new java.io.IOException("Dataset not loaded; reselect directory" +
            (datasetError == null ? "" : ": " + datasetError));
      preparationTrace("media_lookup_started", null);
      selection = playlist.isEncoded()
          ? playlist.encoded(EncodedSegment.key(assignment, inputWidth, inputHeight, inputFps, inputGop))
          : playlist.find(new File(assignment.getString("media_path")).getName());
      preparationTrace("media_lookup_completed", null);
      file = selection.uri;
      try (android.content.res.AssetFileDescriptor fd =
          getContentResolver().openAssetFileDescriptor(file, "r")) {
        if (fd == null) throw new java.io.IOException("Assigned video cannot be opened");
      }
      preparationTrace("media_open_completed", null);
      String app = assignment.getString("application");
      for (int i = 0; i < application.getCount(); i++)
        if (app.equals(application.getItemAtPosition(i).toString())) {
          application.setSelection(i);
          break;
        }
      requestStart();
    } catch (Exception e) {
      event("dataset_assignment_failed", object("reason", e.toString()));
      showDataset("Dataset preparation failed: " + e);
      failCollection("dataset_preparation_failed: " + e);
    }
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request == 8 && result == RESULT_OK) {
      if (enabled || session != null || preparedSession != null) {
        show("Stop streaming before changing the dataset");
        return;
      }
      try {
        Uri tree = data.getData();
        getContentResolver().takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        loadDataset(tree, true);
      } catch (Exception e) {
        datasetError = e.toString();
        event("dataset_load_failed", object("reason", datasetError));
        showDataset("Dataset directory failed: " + datasetError);
      }
    }
  }

  @Override
  protected void onDestroy() {
    destroyed = true;
    datasetGeneration++;
    datasetIo.shutdownNow();
    enabled = false;
    if (media != null) media.close();
    if (session != null) session.close();
    if (preparedSession != null) preparedSession.close();
    if (ws != null) ws.cancel();
    if (callback != null) connectivity.unregisterNetworkCallback(callback);
    cpu.stop();
    timer.shutdownNow();
    probes.shutdownNow();
    networkIo.shutdownNow();
    incoming.execute(() -> {
      receiptIds.close();
      flushEvidence(true);
      log.shutdown();
    });
    incoming.shutdown();
    super.onDestroy();
  }
}
