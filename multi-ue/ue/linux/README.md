Run this client inside the softUE network namespace after OTA attachment. It does not replace the OAI softUE or configure its B210.

Install distro GStreamer Python bindings, plugins-good/bad/ugly/libav, OpenCV and the Python requirements. Source `multi-ue/scripts/environment.sh`, then:

```bash
python -m mec_ue.client --server 192.168.72.136 --source-interface oaitun_ue1 --source /datasets/clip.mp4 --application detection --logs runs/ue-a
```

For continuous prerecorded operation, pass `--source-dir /datasets` instead of a
single source. The client records its random shuffle seed; use `--shuffle-seed N`
to reproduce an order. Each file is used once per cycle, and the next MEC session
is prepared before EOF. Camera sources remain single continuous sessions.

For a camera use `--source 0`. `--source-ip` must be the softUE interface address, not the host management address. `scripts/softue-client.sh` obtains the current IPv4 address automatically from an existing softUE container; use that wrapper for normal runs. Android obtains its cellular address itself. No RNTI is supplied by either client.

### Local outgoing-video preview

The client serves a dashboard at **http://127.0.0.1:8767/** by default. It shows
frames passed to the encoder, before H.264 compression or transmission; it does
not confirm reception at the MEC. The page also shows the UE alias, source,
video ID, frame ID, encoder configuration, and preview errors.

JPEG encoding runs on a separate thread, at most 5 FPS and 960 pixels wide.
Only the latest pending frame is retained. Multiple browsers share the JPEG
encoder. Encoding starts only while an MJPEG viewer is connected and stops
when the last viewer disconnects (an already-running encode may finish).
Hidden tabs disconnect their preview. Stream changes clear the previous image.
Preview CPU work can still affect timing measurements; use `--preview-port 0`
to disable the server entirely for benchmark runs.

Optional client flags:

```bash
--preview-host 127.0.0.1 --preview-port 8767
```

The address is in the **client's network namespace**. If the client runs directly
on the Orin, or in a host-networked container, open the default URL in the Orin's
browser. For a container with its own network namespace, bind with
`--preview-host 0.0.0.0` and publish TCP port 8767 when creating that container,
or use its reachable container IP. Do not change the RTP/control `--source-ip`
to enable dashboard access. Binding to `0.0.0.0` exposes this unauthenticated
preview on reachable interfaces; use it only on your trusted lab network.
The `softue-client.sh` wrapper forwards these preview flags to the client.

### Temporary streaming without radio association

With `mec.calibration.yaml` (development mode, real TorchScript inference), the
MEC advertises `association_required_for_admission: false`. The Linux client
then requests video admission after the MEC acknowledges its IP/alias update,
without waiting for radio association. That acknowledgement does not mark radio
association valid. Periodic acknowledgements also allow retry/reconnection and
playlist progression without EdgeRIC. Real cellular attachment and routes are
still required. Experimental and collection modes retain their existing gates;
older MEC servers that omit the field also retain the association requirement.
Update both the MEC image and the Linux client to enable this behavior.
