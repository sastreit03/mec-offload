# Pixel campaign client for Termux

Python runs the UE on the Pixel. The browser controls the local Python process. The MEC receives the Linux UE control protocol, the campaign's H.264 RTP/UDP traffic, and its existing association probes. The implementation targets staged-media collection. The Android camera, live encoding, and standalone profilers remain available in the Android client.

## Install on each Pixel

1. Download `pixel-termux-ue.zip` to the phone's Download folder, or copy it there from the Spark with `adb -s SERIAL push pixel-termux-ue.zip /sdcard/Download/`.
2. In Termux, install the dependencies while the phone has internet access. Grant the shared-storage permission when prompted. Extract the bundle into Termux home.

```sh
pkg install python python-pip unzip
termux-setup-storage
mkdir -p ~/mec-pixel
cd ~/mec-pixel
unzip ~/storage/downloads/pixel-termux-ue.zip
python -m pip install -r multi-ue/ue/termux/requirements.txt
```

3. Use the existing RAN Spark staging procedure in the dataset collection tutorial. The default handset directory is `/sdcard/Movies/AetheredgeEncoded`. An existing verified asset can be used by either client. Stop the APK client before using the same campaign alias in Termux.
4. Select the testbed cellular network. For the initial test, turn off Wi-Fi and VPN routing to the MEC. Automatic address selection follows Android's route to the configured MEC IPv4 address. The optional source IP binds the sockets to that address. The Termux program does not request an Android cellular Network handle.
5. Start the local service from Termux. Open the displayed address in Chrome on the **same Pixel**.

```sh
cd ~/mec-pixel
sh multi-ue/ue/termux/run.sh
```

Open `http://127.0.0.1:8767/`. Set the MEC IPv4 address, port, UE alias and encoded media directory. Tap **Connect and stream**. The server assignment controls the application and bitrate during collection. The UI retains connection settings for next time. Each service launch starts disconnected. Reloading the page displays the running service's state. After an explicit Connect, the client retries a lost control connection until Disconnect or shutdown. Ctrl-C stops the local service.

## Read the checks

| Check | Evidence |
| --- | --- |
| WebSocket | Connected after the MEC hello for this connection. Verify the displayed source IP is the handset's testbed IP. |
| Association probes | Scheduled when the MEC issues a probe command. Sent when the client completes the UDP probe pattern. |
| MEC association report | Validated when the MEC reports a valid association for this connection. A report can later change to Unvalidated. |
| RTP media | Waiting before assignment. UDP ready means the assigned file passed verification and the socket opened. UDP submitted means a full frame was handed to the socket. Completed result received means the MEC returned a completed result matching a locally transmitted frame. |

The checks use normal campaign traffic. The client waits for the scenario start before transmitting any video. Browser polling runs once per second, so short-lived states can pass between updates. A closed browser tab leaves the Termux service in its current state.

## Collect evidence

After the MEC has drained the final attempt, leave this service running and use
the **Termux client** evidence block in
[the collection tutorial](../../docs/dataset-collection-tutorial.md#pixel-evidence)
on the RAN Spark. It forwards the localhost service over ADB, disconnects and
flushes the client, then downloads `/api/events` and applies the existing run
filter. No phone-side copy command is needed. The endpoint requires a
disconnected client in the current service session and exposes only its event
log. Do not stop the service before downloading.

For an older running service without `/api/events`, use this manual fallback.
Disconnect after the MEC has drained the final attempt. The UI shows the evidence directory. The default for the first alias and default MEC is `~/.local/state/mec-pixel/192.168.72.136-8765/pixel1/`. Identity and SQLite replay state are private to the host, port, and alias. On the Pixel, copy the event log to shared storage, adjusting the source for the settings used.

```sh
cp ~/.local/state/mec-pixel/192.168.72.136-8765/pixel1/events.jsonl ~/storage/downloads/pixel1-events.jsonl
```

On the Spark, use the tutorial's existing temporary pull directory and review directory. Substitute the Termux export for the APK-specific ADB path.

```sh
adb -s "$SERIAL" pull "/sdcard/Download/${ALIAS}-events.jsonl" "$PIXEL_CAPTURE/events.jsonl"
python3 tools/filter_run_events.py "$PIXEL_CAPTURE/events.jsonl" "$REVIEW/pixel/$ALIAS/events.jsonl"
```

The two SLO endpoints are `tx_mono_ns` before packetization and `receipt_mono_ns` when Python obtains a complete WebSocket message. The receipt timestamp precedes the ordered processing queue and SQLite deduplication. Segmentation payloads still travel over the network. The metadata parser stops at the final payload field for the current wire format. Older payload-first replay messages are accepted. The result worker, persistent event writer and paced media sender run independently of browser refreshes. Process CPU counters include Python threads and exclude Chrome. Writes flush every available batch with a 25 ms batching window. An abrupt process kill can lose buffered evidence.

## Run the included test

On a Linux host or in Termux with Python and the pinned dependency installed, from the extracted directory:

```sh
cd multi-ue/ue/termux
PYTHONPATH=../linux/src:../../contracts/python python -m unittest -v test_campaign
```

The test uses a temporary directory, a mock MEC, loopback UDP, and the actual local HTTP service. A field check still requires a handset, a staged campaign asset and the testbed.
