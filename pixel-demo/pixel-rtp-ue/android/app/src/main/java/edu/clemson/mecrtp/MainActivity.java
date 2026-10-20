package edu.clemson.mecrtp;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.SurfaceTexture;
import android.net.*;
import android.os.*;
import android.util.Base64;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import java.io.*;
import java.util.concurrent.*;
import okhttp3.*;
import org.json.JSONObject;
import java.net.Proxy;

public final class MainActivity extends Activity {
    private static final String LOCAL = "https://appassets.androidplatform.net/index.html";
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ScheduledExecutorService polling = Executors.newSingleThreadScheduledExecutor();
    private final OkHttpClient http = new OkHttpClient.Builder().connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS).pingInterval(10, TimeUnit.SECONDS).proxy(Proxy.NO_PROXY).build();
    private WebView web;
    private TextureView preview;
    private RtpCamera camera;
    private ConnectivityManager connectivity;
    private ConnectivityManager.NetworkCallback networkCallback;
    private WebSocket control;
    private volatile String base = "";
    private volatile boolean connected, previewEnabled = true, networkReady;
    private volatile int connectionGeneration;
    private String host;
    private int controlPort;
    private String controlPath = "/ws/ue";
    private volatile JSONObject latestVideo, latestQuestion;
    private volatile String lastPreviewError = "";
    private volatile int controlAttempt;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // Insets remain outside the web view, including on targetSdk 35 edge-to-edge devices.
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.HORIZONTAL);
        root.setBackgroundColor(0xff0c1321);
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom()); return insets;
        });
        LinearLayout pane = new LinearLayout(this); pane.setOrientation(LinearLayout.VERTICAL);
        TextView label = new TextView(this); label.setText("  UE DECODED H.264 · transmitted video"); label.setTextColor(0xffcbd5e1);
        label.setPadding(8, 16, 8, 16); pane.addView(label);
        preview = new TextureView(this);
        pane.addView(preview, new LinearLayout.LayoutParams(-1, 0, 1));
        TextView hint = new TextView(this); hint.setText("  Hold the Pixel in landscape.\n  Keep this app open while streaming.");
        hint.setTextColor(0xff94a3b8); hint.setPadding(8, 12, 8, 12); pane.addView(hint);
        root.addView(pane, new LinearLayout.LayoutParams(0, -1, 0.34f));
        web = new WebView(this); root.addView(web, new LinearLayout.LayoutParams(0, -1, 0.66f));
        setContentView(root);
        connectivity = (ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);
        camera = new RtpCamera(this, this::cameraEvent);
        WebSettings settings = web.getSettings(); settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true); settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
        // Serve only bundled HTML at a fixed origin. Native bridge performs every network request.
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView w, WebResourceRequest r) { return !LOCAL.equals(r.getUrl().toString()); }
            @Override public WebResourceResponse shouldInterceptRequest(WebView w, WebResourceRequest r) {
                if (LOCAL.equals(r.getUrl().toString())) {
                    try { return new WebResourceResponse("text/html", "UTF-8", getAssets().open("index.html")); }
                    catch (IOException e) { return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream("Missing dashboard".getBytes())); }
                }
                return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
            }
        });
        web.addJavascriptInterface(new Bridge(), "Pixel"); web.loadUrl(LOCAL);
        polling.scheduleWithFixedDelay(this::poll, 1, 500, TimeUnit.MILLISECONDS);
    }
    private void emit(String raw) {
        main.post(() -> { if (web != null) web.evaluateJavascript("window.onNative&&window.onNative(" + JSONObject.quote(raw) + ");", null); });
    }
    private void event(String type, String detail) {
        try { emit(new JSONObject().put("type", type).put("detail", detail).toString()); } catch (Exception ignored) { }
    }
    private void cameraEvent(JSONObject e) {
        emit(e.toString());
        if ("stream_started".equals(e.optString("type"))) {
            try { latestVideo = e.getJSONObject("video"); if(connected) sendConfig(); }
            catch (Exception ignored) { }
        }
    }
    public final class Bridge {
        @JavascriptInterface public void connect(String raw) {
            main.post(() -> {
                try {
                    JSONObject p = new JSONObject(raw); String ip = p.getString("host");
                    if (!ip.matches("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")) throw new IllegalArgumentException("Use the MEC IPv4 address");
                    for (String part : ip.split("\\.")) if (Integer.parseInt(part) > 255) throw new IllegalArgumentException("Invalid IPv4 address");
                    int port = p.getInt("control_port"); if (port < 1 || port > 65535) throw new IllegalArgumentException("Invalid control port");
                    String path=p.optString("control_path","/ws/ue");
                    if(!path.matches("/[a-zA-Z0-9/_-]+"))throw new IllegalArgumentException("Invalid WebSocket path");
                    MainActivity.this.disconnect(); host = ip; controlPort = port;
                    controlPath=path;
                    final int run = connectionGeneration;
                    event("connecting", "Requesting cellular PDU network");
                    NetworkRequest request = new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
                        .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build();
                    networkCallback = new ConnectivityManager.NetworkCallback() {
                        @Override public void onAvailable(Network network) {
                            main.post(() -> {
                                if (run != connectionGeneration) return;
                                if (!connectivity.bindProcessToNetwork(network)) { event("native_error", "Cannot bind cellular network"); disconnect(); return; }
                                http.connectionPool().evictAll();
                                networkReady=true;base="http://"+host+":"+controlPort;
                                LinkProperties lp=connectivity.getLinkProperties(network);
                                event("network", lp==null?network.toString():lp.toString());openControl(run);
                            });
                        }
                        @Override public void onLost(Network network) {
                            main.post(() -> { if (run == connectionGeneration) { event("native_error", "Cellular network lost"); disconnect(); } });
                        }
                        @Override public void onUnavailable() {
                            main.post(() -> { if (run == connectionGeneration) { event("native_error", "No cellular PDU network; verify attachment/mobile data"); disconnect(); } });
                        }
                    };
                    connectivity.requestNetwork(request, networkCallback, 15000);
                } catch (Exception e) { event("native_error", e.toString()); MainActivity.this.disconnect(); }
            });
        }
        @JavascriptInterface public void disconnect() { main.post(MainActivity.this::disconnect); }
        @JavascriptInterface public void start(String raw) {
            main.post(() -> {
                try {
                    if (!networkReady) throw new IllegalStateException("Connect cellular network first");
                    if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, 1);
                        event("native_error", "Grant Camera permission, then tap Start again"); return;
                    }
                    if (!preview.isAvailable()) throw new IllegalStateException("Camera preview not ready");
                    JSONObject p = new JSONObject(raw);
                    int width = p.getInt("width"), height = p.getInt("height"), fps = p.getInt("fps");
                    int kbps=p.getInt("bitrate_kbps"), port=p.getInt("video_port"),gop=p.getInt("gop_frames"),mtu=p.getInt("rtp_mtu");
                    if(width<160||width>3840||height<120||height>2160||((width|height)&1)!=0||fps<1||fps>30)
                        throw new IllegalArgumentException("Use even dimensions, width 160-3840, height 120-2160, FPS 1-30");
                    if(kbps<50||kbps>20000||gop<1||gop>300||mtu<576||mtu>1400||port<1||port>65535)
                        throw new IllegalArgumentException("Invalid bitrate/GOP/MTU/UDP port");
                    SurfaceTexture texture = preview.getSurfaceTexture(); texture.setDefaultBufferSize(width, height);
                    camera.start(host,p,new Surface(texture));
                } catch (Exception e) { event("native_error", e.toString()); }
            });
        }
        @JavascriptInterface public void stop() { camera.stop(); }
        @JavascriptInterface public void send(String raw) { MainActivity.this.send(raw); }
        @JavascriptInterface public void decodedPreview(boolean value) { previewEnabled = value; }
    }
    private void openControl(int run) {
        final int attempt=++controlAttempt;
        if(control!=null)control.cancel();
        base = "http://" + host + ":" + controlPort;
        control = http.newWebSocket(new Request.Builder().url("ws://" + host + ":" + controlPort + controlPath).build(), new WebSocketListener() {
            @Override public void onOpen(WebSocket ws, Response response) {
                if (run != connectionGeneration || attempt!=controlAttempt) { ws.cancel(); return; }
                connected = true; event("connected", base);
                ws.send("{\"type\":\"ue_hello\",\"client\":\"pixel7-rtp\",\"protocol_version\":1}");
                if(latestVideo!=null)sendConfig();if(latestQuestion!=null)ws.send(latestQuestion.toString());
            }
            @Override public void onMessage(WebSocket ws, String text) { if (run == connectionGeneration && attempt==controlAttempt) emit(text); }
            @Override public void onClosing(WebSocket ws, int code, String reason) { ws.close(code, reason); }
            @Override public void onFailure(WebSocket ws, Throwable t, Response response) {
                main.post(() -> { if (run == connectionGeneration && attempt==controlAttempt) reconnectControl(run,attempt,"Control: "+t); });
            }
            @Override public void onClosed(WebSocket ws, int code, String reason) {
                main.post(() -> { if (run == connectionGeneration && attempt==controlAttempt) reconnectControl(run,attempt,"Control closed"); });
            }
        });
    }
    private void send(String raw) {
        try{JSONObject m=new JSONObject(raw);if("question".equals(m.optString("type")))latestQuestion=m;}catch(Exception e){event("native_error","Invalid control JSON");return;}
        WebSocket ws = control;
        if (!connected || ws == null || !ws.send(raw)) event("question_queued", "Control reconnecting; latest question will be resent");
    }
    private void sendConfig(){
        try{WebSocket ws=control;if(connected&&ws!=null&&latestVideo!=null)ws.send(new JSONObject().put("type","stream_config")
            .put("sent_unix_ns",System.currentTimeMillis()*1_000_000L).put("video",latestVideo).toString());}catch(Exception ignored){}
    }
    private void reconnectControl(int run,int attempt,String detail){
        connected=false;event("control_reconnecting",detail);
        main.postDelayed(()->{if(run==connectionGeneration&&networkReady&&attempt==controlAttempt)openControl(run);},2000);
    }
    private void disconnect() {
        connectionGeneration++; connected = networkReady = false; base = ""; camera.stop();
        if (control != null) { control.cancel(); control = null; }
        if (networkCallback != null) {
            try { connectivity.unregisterNetworkCallback(networkCallback); } catch (Exception ignored) { }
            networkCallback = null;
        }
        connectivity.bindProcessToNetwork(null); http.connectionPool().evictAll(); event("disconnected", "Disconnected");
    }
    private int polls;
    private long previewSequence;
    private void poll() {
        if (!networkReady) return;
        int run = connectionGeneration; String target = base;
        try {
            if (++polls % 2 == 1) {
                try (Response r = http.newCall(new Request.Builder().url(target + "/api/status").build()).execute()) {
                    if (r.isSuccessful() && r.body() != null && run == connectionGeneration){
                        JSONObject status=new JSONObject(r.body().string());
                        previewSequence=status.getJSONObject("state").getJSONObject("receiver").optLong("preview_sequence",0);
                        emit(new JSONObject().put("type","mec_status").put("status",status).toString());
                    }else if(!r.isSuccessful())throw new IOException("Status HTTP "+r.code());
                }
            }
            if (previewEnabled && previewSequence>0) {
                try (Response r = http.newCall(new Request.Builder().url(target + "/preview.mjpg").build()).execute()) {
                    if (r.isSuccessful() && r.body() != null && run == connectionGeneration) {
                        byte[] bytes=MjpegReader.firstFrame(r.body().byteStream());
                        if(run!=connectionGeneration || !networkReady)return;
                        emit(new JSONObject().put("type", "decoded_frame")
                            .put("jpeg", Base64.encodeToString(bytes, Base64.NO_WRAP)).toString());
                        lastPreviewError="";
                    }else if(!r.isSuccessful())throw new IOException("Preview HTTP "+r.code());
                }
            }
        } catch (Exception e) {
            if(run==connectionGeneration&&!e.toString().equals(lastPreviewError)){lastPreviewError=e.toString();event("preview_error",lastPreviewError);}
        }
    }
    @Override protected void onPause() { super.onPause(); disconnect(); }
    @Override protected void onDestroy() {
        disconnect(); polling.shutdownNow(); camera.destroy();
        http.dispatcher().cancelAll(); http.dispatcher().executorService().shutdown();
        web.removeJavascriptInterface("Pixel"); web.destroy(); web = null; super.onDestroy();
    }
}
