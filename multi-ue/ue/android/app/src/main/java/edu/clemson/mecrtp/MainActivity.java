package edu.clemson.mecrtp;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.net.*;
import android.os.*;
import android.provider.DocumentsContract;
import android.widget.*;
import java.io.*;
import java.net.*;
import java.util.concurrent.*;
import okhttp3.*;
import org.json.*;

/** Foreground research client: cellular-bound control, media, automatic probes and recovery. */
public final class MainActivity extends Activity {
  private final Handler ui = new Handler(Looper.getMainLooper());
  private final ClockMap clock = new ClockMap();
  private final ScheduledExecutorService timer = Executors.newScheduledThreadPool(2);
  private final ExecutorService probes = Executors.newSingleThreadExecutor();
  private final ThreadPoolExecutor log =
      new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(4096));
  private ConnectivityManager connectivity;
  private ConnectivityManager.NetworkCallback callback;
  private volatile Network network;
  private volatile WebSocket ws;
  private EditText host;
  private Spinner application;
  private TextView status;
  private Uri file;
  private VideoPlaylist playlist;
  private VideoPlaylist.Selection selection, successorSelection;
  private final java.util.List<StreamSession> retiring = new CopyOnWriteArrayList<>();
  private android.database.sqlite.SQLiteDatabase receiptIds;
  private StreamSession session;
  private StreamSession preparedSession;
  private AvcSource media;
  private String uid, connection, run;
  private String campaignId, scenarioId, attemptId;
  private int inputWidth = 1280, inputHeight = 720, inputFps = 30;
  private String cellularIp = "";
  private JSONObject startRequest;
  private JSONObject successorRequest;
  private boolean awaitingSuccessor;
  private boolean collectionMode;
  private double successorPrepareS;
  private volatile boolean enabled;
  private int generation;
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
          Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
          i.addFlags(
              Intent.FLAG_GRANT_READ_URI_PERMISSION
                  | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
          startActivityForResult(i, 8);
        });
    root.addView(choose);
    Button camera = new Button(this);
    camera.setText("Use rear camera");
    camera.setOnClickListener(
        v -> {
          file = null;
          playlist = null;
          selection = null;
          show("Rear camera selected");
        });
    root.addView(camera);
    Button start = new Button(this);
    start.setText("Connect and stream");
    start.setOnClickListener(
        v -> {
          if (enabled) return;
          if (file == null
              && checkSelfPermission(Manifest.permission.CAMERA)
                  != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, 9);
            return;
          }
          prefs.edit().putString("host", host.getText().toString().trim()).apply();
          enabled = true;
          requestCellular();
        });
    root.addView(start);
    Button stop = new Button(this);
    stop.setText("End video");
    stop.setOnClickListener(v -> end());
    root.addView(stop);
    status = new TextView(this);
    root.addView(status);
    setContentView(root);
    getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    timer.scheduleAtFixedRate(
        () -> {
          try {
            if (ws != null) {
              send(
                  new JSONObject()
                      .put("type", "time_sync")
                      .put("client_mono_ns", System.nanoTime()));
              association();
            }
            if (session != null) session.deadlines();
            for (StreamSession old : retiring) old.deadlines();
          } catch (Exception e) {
            event("client_error", object("reason", e.toString()));
          }
        },
        1,
        10,
        TimeUnit.SECONDS);
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
    ui.post(() -> status.setText(s));
  }

  // Persist lifecycle sequence numbers so retries/reconnects do not reuse command IDs.
  private synchronized long seq() {
    clientSeq++;
    prefs.edit().putLong("seq", clientSeq).commit();
    return clientSeq;
  }

  // Queue experiment evidence off the media and UI paths.
  private void event(String type, JSONObject value) {
    try {
      JSONObject record =
          new JSONObject(value.toString())
              .put("type", type)
              .put("run_id", run)
              .put("ue_id", uid)
              .put("monotonic_ns", System.nanoTime());
      if (!record.has("campaign_id"))
        record.put("campaign_id", campaignId == null ? JSONObject.NULL : campaignId);
      if (!record.has("scenario_id"))
        record.put("scenario_id", scenarioId == null ? JSONObject.NULL : scenarioId);
      if (!record.has("attempt_id"))
        record.put("attempt_id", attemptId == null ? JSONObject.NULL : attemptId);
      log.execute(
          () -> {
            try (FileWriter f =
                new FileWriter(new File(getExternalFilesDir(null), "events.jsonl"), true)) {
              f.write(record.toString() + "\n");
            } catch (Exception e) {
              enabled = false;
              show("Evidence write failed: " + e);
            }
          });
      if (type.equals("media_end") && session != null)
        ui.post(() -> mediaEnded(value));
      if (type.equals("successor_needed") && playlist != null)
        ui.post(() -> prepareSuccessor(value));
      if (type.equals("plan_unapplied")) send(new JSONObject(value.toString()).put("type", type));
      if (type.equals("sender_error")) show(value.toString());
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
            ui.post(
                () -> {
                  network = n;
                  try {
                    if (session != null) session.network(n);
                    if (preparedSession != null) preparedSession.network(n);
                  } catch (Exception e) {
                    event("media_rebind_error", object("reason", e.toString()));
                  }
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
                    boolean changed = !cellularIp.isEmpty() && !cellularIp.equals(ip);
                    cellularIp = ip;
                    if (changed) {
                      try {
                        if (session != null) session.network(n);
                        if (preparedSession != null) preparedSession.network(n);
                      } catch (Exception e) {
                        event("media_rebind_error", object("reason", e.toString()));
                      }
                      connect();
                    }
                  }
                });
          }

          public void onLost(Network n) {
            ui.post(
                () -> {
                  if (n.equals(network)) {
                    network = null;
                    if (ws != null) ws.cancel();
                    ws = null;
                    show("Cellular lost; waiting for automatic recovery");
                  }
                });
          }
        };
    connectivity.requestNetwork(
        new NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
            .build(),
        callback);
  }

  private void connect() {
    if (!enabled || network == null) return;
    final int g = ++generation;
    String address = prefs.getString("host", "");
    OkHttpClient client =
        new OkHttpClient.Builder()
            .socketFactory(network.getSocketFactory())
            .dns(name -> java.util.Arrays.asList(network.getAllByName(name)))
            .proxy(java.net.Proxy.NO_PROXY)
            .pingInterval(10, TimeUnit.SECONDS)
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
              public void onMessage(WebSocket socket, String raw) {
                long receipt = System.nanoTime();
                ui.post(
                    () -> {
                      if (g != generation) return;
                      try {
                        message(new JSONObject(raw), receipt);
                      } catch (Exception e) {
                        event("protocol_error", object("reason", e.toString()));
                        show(e.toString());
                      }
                    });
              }

              public void onFailure(WebSocket socket, Throwable e, Response r) {
                retry(g);
              }

              public void onClosed(WebSocket socket, int c, String reason) {
                retry(g);
              }
            });
  }

  private void retry(int g) {
    ui.postDelayed(
        () -> {
          if (g == generation && enabled) {
            ws = null;
            show("Control reconnecting");
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

  private void message(JSONObject m, long receipt) throws Exception {
    String type = m.getString("type");
    switch (type) {
      case "mec_hello":
        if (session != null
            && (!m.optString("active_video_id").equals(session.video())
                || run != null && !run.equals(m.getString("run_id")))) {
          if (media != null) media.close();
          if (session != null) session.close();
          session = null;
          media = null;
          startRequest = null;
        }
        run = m.getString("run_id");
        uid = m.getString("ue_id");
        setContext(m);
        connection = m.getString("connection_id");
        // Use the MEC's validated input profile for configurable FPS experiments.
        JSONObject inputProfile = m.optJSONObject("input_profile");
        if (inputProfile != null) {
          inputWidth = inputProfile.getInt("width");
          inputHeight = inputProfile.getInt("height");
          inputFps = inputProfile.getInt("fps");
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
        break;
      case "scenario_context":
        if (session != null || preparedSession != null)
          throw new IllegalStateException("scenario context changed during active media");
        setContext(m);
        startRequest = null;
        successorRequest = null;
        event("scenario_context", new JSONObject());
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
        if (collectionMode && !contextReady()) break;
        if (m.getBoolean("valid") && successorRequest != null) send(successorRequest);
        if (enabled && m.getBoolean("valid") && session == null) {
          if (startRequest == null)
            startRequest =
                new JSONObject()
                    .put("type", "video_start")
                    .put("ue_id", uid)
                    .put("client_seq", seq())
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
          send(startRequest);
        }
        break;
      case "video_response":
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
            preparedSession.network(network);
            if (awaitingSuccessor) startPreparedSuccessor();
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
          session = new StreamSession(m, clock, this::event, getExternalFilesDir(null));
          session.network(network);
          media = new AvcSource(this, session, this::event, playlist == null ? 0 : successorPrepareS);
          media.start(file);
          show("Streaming " + session.video());
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
            show(m.toString());
            if (media != null) media.close();
            retire(session);
            session = null;
            startRequest = null;
            if (playlist != null && preparedSession != null) {
              startPreparedSuccessor();
            } else if (playlist != null) {
              // A failed early preparation retries through ordinary admission.
              if (successorSelection == null) {
                successorSelection = playlist.next();
                recordSelection(successorSelection);
              }
              selection = successorSelection;
              file = selection.uri;
              successorSelection = null;
            } else if (playlist == null && !m.getString("status").equals("EXPIRED")) {
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
          successorRequest = null;
          event("successor_not_prepared", object("reason", m.getString("status")));
        }
        break;
      // Stable result IDs deduplicate reconnect replay before recording a receipt.
      case "frame_result":
        String id = m.getString("result_id");
        try (android.database.Cursor cursor =
            receiptIds.rawQuery("SELECT 1 FROM seen WHERE id=?", new String[] {id})) {
          if (cursor.moveToFirst()) break;
        }
        receiptIds.execSQL("INSERT INTO seen VALUES (?)", new Object[] {id});
        StreamSession owner =
            session != null && session.video().equals(m.getString("video_id")) ? session : null;
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
        show("Frame " + m.getLong("frame_id") + " · " + m.getString("outcome"));
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
      send(
          new JSONObject()
              .put("type", "video_end")
              .put("ue_id", uid)
              .put("video_id", session.video())
              .put("client_seq", seq())
              .put("final_frame_id", value.getLong("final_frame_id")));
      awaitingSuccessor = playlist != null;
      if (preparedSession != null) startPreparedSuccessor();
      else if (playlist != null)
        event("successor_response_pending", object("reason", "response_pending_at_eof"));
    } catch (Exception e) {
      event("client_error", object("reason", e.toString()));
    }
  }

  private void startPreparedSuccessor() throws Exception {
    if (preparedSession == null || successorSelection == null) return;
    if (session != null) retire(session);
    session = preparedSession;
    selection = successorSelection;
    file = selection.uri;
    preparedSession = null;
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
        } else if ((mime != null && mime.startsWith("video/"))
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
      // Explicit user stop disables automatic prerecorded continuation.
      playlist = null;
      awaitingSuccessor = false;
      if (preparedSession != null) preparedSession.close();
      preparedSession = null;
      if (media != null) media.close();
      if (session != null && session.lastFrame() >= 0)
        send(
            new JSONObject()
                .put("type", "video_end")
                .put("ue_id", uid)
                .put("video_id", session.video())
                .put("client_seq", seq())
                .put("final_frame_id", session.lastFrame()));
      else {
        enabled = false;
        if (ws != null) ws.close(1000, "stopped");
      }
    } catch (Exception e) {
      show(e.toString());
    }
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request == 8 && result == RESULT_OK) {
      try {
        Uri tree = data.getData();
        getContentResolver()
            .takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        java.util.List<VideoPlaylist.Selection> files = new java.util.ArrayList<>();
        collectVideos(tree, DocumentsContract.getTreeDocumentId(tree), files);
        playlist = new VideoPlaylist(files);
        selection = playlist.next();
        file = selection.uri;
        event(
            "playlist_initialized",
            new JSONObject().put("seed", playlist.seed).put("videos", playlist.size()));
        recordSelection(selection);
        show("Dataset directory: " + playlist.size() + " videos");
      } catch (Exception e) {
        playlist = null;
        file = null;
        show("Dataset directory failed: " + e);
      }
    }
  }

  @Override
  protected void onDestroy() {
    enabled = false;
    if (media != null) media.close();
    if (session != null) session.close();
    if (preparedSession != null) preparedSession.close();
    if (ws != null) ws.cancel();
    if (callback != null) connectivity.unregisterNetworkCallback(callback);
    timer.shutdownNow();
    probes.shutdownNow();
    log.shutdown();
    super.onDestroy();
  }
}
