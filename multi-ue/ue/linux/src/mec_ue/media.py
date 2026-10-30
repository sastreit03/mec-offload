import threading
import time
import socket
import secrets
from dataclasses import replace
from mec_contracts.rtp import Descriptor, packetize, split_annexb, canonical_au


# Feed raw frames through a low-latency H.264 encoder with explicit keyframe requests.
class Encoder:
    def __init__(self, width, height, fps, bitrate):
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo

        Gst.init(None)
        self.Gst = Gst
        self.GstVideo = GstVideo
        self.pipeline = Gst.parse_launch(
            f"appsrc name=raw is-live=true format=time block=true max-bytes={width * height * 3 * 2} "
            f'caps="video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1" ! videoconvert ! '
            f"x264enc name=encoder tune=zerolatency speed-preset=veryfast bitrate={bitrate // 1000} key-int-max=300 bframes=0 byte-stream=true ! "
            "h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=encoded max-buffers=1 drop=false sync=false wait-on-eos=false enable-last-sample=false"
        )
        self.src = self.pipeline.get_by_name("raw")
        self.sink = self.pipeline.get_by_name("encoded")
        self.encoder = self.pipeline.get_by_name("encoder")
        self.pipeline.set_state(Gst.State.PLAYING)

    def frame(self, bgr, index, fps, keyframe=False, bitrate=None):
        if bitrate:
            self.encoder.set_property("bitrate", int(bitrate) // 1000)
        if keyframe:
            event = self.GstVideo.video_event_new_downstream_force_key_unit(
                self.Gst.CLOCK_TIME_NONE,
                self.Gst.CLOCK_TIME_NONE,
                self.Gst.CLOCK_TIME_NONE,
                True,
                index,
            )
            self.encoder.get_static_pad("sink").send_event(event)
        raw = bgr.tobytes()
        buf = self.Gst.Buffer.new_allocate(None, len(raw), None)
        buf.fill(0, raw)
        buf.pts = round(index * 1e9 / fps)
        buf.duration = round(1e9 / fps)
        if self.src.emit("push-buffer", buf) != self.Gst.FlowReturn.OK:
            raise RuntimeError("encoder flow error")
        sample = self.sink.emit("try-pull-sample", 2 * self.Gst.SECOND)
        if sample is None:
            raise RuntimeError("encoder timed out")
        b = sample.get_buffer()
        return split_annexb(b.extract_dup(0, b.get_size()))

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


# A dedicated thread owns capture, encoding, pacing, and UDP transmission.
class Streamer:
    def __init__(
        self,
        response,
        source,
        source_ip,
        clock,
        event,
        prepare_s=0,
        source_start_frame=0,
        frame_count=None,
    ):
        self.response = response
        self.source = source
        self.source_ip = source_ip
        self.clock = clock
        self.event = event
        self.prepare_s = prepare_s
        self.source_start_frame = source_start_frame
        self.frame_count = frame_count
        self.current = dict(response["config"])
        self.pending = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.control_version = self.current.get("control_version", 0)
        self.pause_at = None
        self.pause_version = 0
        self.frame_id = 0
        self.gop_id = 0
        self.gop_frame = 0
        self.last_frame = -1
        self.thread = threading.Thread(
            target=self.run, name="mec-rtp-sender", daemon=True
        )

    # Queue future commands under the lock shared with the media thread.
    def command(self, c):
        with self.lock:
            if c.get("control_version", 0) <= self.control_version:
                return
            if c["config_version"] <= self.current["config_version"]:
                return
            if any(x["config_version"] == c["config_version"] for x in self.pending):
                return
            if c["boundary_unix_ns"] <= self.clock.unix_ns():
                self.event(
                    "plan_unapplied",
                    dict(command_id=c["command_id"], reason="late_command"),
                )
                return
            self.control_version = c["control_version"]
            # A resume arriving before its suspension still carries the stop interval.
            if (
                c.get("stop_unix_ns") is not None
                and c["suspension_version"] > self.pause_version
            ):
                self.pause_at = c["stop_unix_ns"]
                self.pause_version = c["suspension_version"]
            self.pending.append(c)
            self.pending.sort(key=lambda x: x["boundary_unix_ns"])

    def suspend(self, command):
        with self.lock:
            version = command["control_version"]
            if version <= self.control_version:
                return
            self.control_version = version
            self.pause_at = command["effective_unix_ns"]
            self.pause_version = version
            self.event(
                "suspension",
                dict(
                    video_id=self.response["video_id"],
                    control_version=version,
                    effective_unix_ns=self.pause_at,
                    command_id=command["command_id"],
                ),
            )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()

    def run(self):
        import cv2

        cfg = self.current
        encoder = None
        sock = None
        try:
            cap = cv2.VideoCapture(
                int(self.source) if self.source.isdecimal() else self.source
            )
            if not cap.isOpened():
                raise RuntimeError("camera/video could not be opened")
            if self.source_start_frame:
                # Aetheredge: prerecorded campaign segments begin at the exact
                # manifest frame instead of depending on playlist history.
                cap.set(cv2.CAP_PROP_POS_FRAMES, self.source_start_frame)
                actual = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                if actual != self.source_start_frame:
                    raise RuntimeError(
                        f"source seek mismatch: {actual} != {self.source_start_frame}"
                    )
            total_frames = 0
            if self.prepare_s > 0 and not self.source.isdecimal():
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            preparation_sent = False
            encoder = Encoder(
                cfg["width"], cfg["height"], cfg["fps"], cfg["bitrate_bps"]
            )
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.source_ip, 0))
            sock.connect(
                (self.response["endpoint"]["host"], self.response["endpoint"]["port"])
            )
            ssrc = secrets.randbits(32)
            seq = secrets.randbits(16)
            rtp_base = secrets.randbits(32)
            # Local setup may finish after a prepared boundary; do not burst to catch up.
            release = max(cfg["boundary_unix_ns"], self.clock.unix_ns())
            force = True
            while not self.stopped.is_set():
                if self.frame_count is not None and self.frame_id >= self.frame_count:
                    break
                if (
                    self.prepare_s > 0
                    and total_frames > 0
                    and not preparation_sent
                    and total_frames - self.frame_id
                    <= max(1, round(self.prepare_s * cfg["fps"]))
                ):
                    remaining = max(0, total_frames - self.frame_id)
                    self.event(
                        "successor_needed",
                        dict(
                            video_id=self.response["video_id"],
                            expected_start_unix_ns=release
                            + round(remaining * 1e9 / cfg["fps"]),
                        ),
                    )
                    preparation_sent = True
                delay = (release - self.clock.unix_ns()) / 1e9
                if delay > 0 and self.stopped.wait(delay):
                    break
                with self.lock:
                    # A stalled capture can leave the planned release behind real time.
                    now = self.clock.unix_ns() if self.pending else release
                    while (
                        self.pending and self.pending[0]["boundary_unix_ns"] <= now
                    ):
                        nxt = self.pending.pop(0)
                        # A bounded short transition GoP reaches the explicitly commanded phase.
                        if now - nxt["boundary_unix_ns"] > round(1e9 / cfg["fps"]):
                            self.event(
                                "plan_unapplied",
                                dict(
                                    command_id=nxt["command_id"],
                                    reason="boundary_missed",
                                ),
                            )
                            continue
                        cfg = dict(nxt)
                        self.current = cfg
                        self.gop_frame = 0
                        self.gop_id += 1
                        force = True
                        if nxt.get("control_version", 0) > self.pause_version:
                            self.pause_at = None
                if self.pause_at and release >= self.pause_at:
                    # Consume the source frame and its ID so resumed metadata exposes
                    # the video-time gap that ByteTrack must age through.
                    if not cap.grab():
                        break
                    self.frame_id += 1
                    release += round(1e9 / cfg["fps"])
                    continue
                if sock.getsockname()[0] != self.source_ip:
                    sock.close()
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.bind((self.source_ip, 0))
                    sock.connect(
                        (
                            self.response["endpoint"]["host"],
                            self.response["endpoint"]["port"],
                        )
                    )
                ok, bgr = cap.read()
                if not ok:
                    break
                capture = time.monotonic_ns()
                bgr = cv2.resize(bgr, (cfg["width"], cfg["height"]))
                nals = encoder.frame(
                    bgr,
                    self.frame_id,
                    cfg["fps"],
                    keyframe=force or self.gop_frame == 0,
                    bitrate=cfg["bitrate_bps"],
                )
                kind = 1 if any(n[0] & 31 == 5 for n in nals) else 3
                if (force or self.gop_frame == 0) and kind != 1:
                    raise RuntimeError(
                        "encoder did not produce commanded decodable keyframe; configuration not mislabeled"
                    )
                force = False
                tx = time.monotonic_ns()
                # Timestamp validity flags distinguish mapped wall times from unavailable values.
                valid = self.clock.valid()
                d = Descriptor(
                    self.response["video_id"],
                    int(self.response["media_epoch"]),
                    self.frame_id,
                    self.gop_id,
                    cfg["config_version"],
                    cfg["plan_version"],
                    kind,
                    len(canonical_au(nals)),
                    capture,
                    tx,
                    self.clock.to_unix(capture) if valid else 0,
                    self.clock.to_unix(tx) if valid else 0,
                    15 if valid else 3,
                )
                packets = list(
                    packetize(
                        nals,
                        d,
                        seq,
                        rtp_base + round(self.frame_id * 90000 / cfg["fps"]),
                        ssrc,
                        self.response["rtp"]["mtu"],
                    )
                )
                # Timestamp sampled before transport submission. Packetization overhead is included in UE SLO.
                try:
                    for p in packets:
                        sock.send(p)
                except OSError as ex:
                    self.event(
                        "transport_submit_error",
                        dict(
                            frame_id=self.frame_id, video_id=d.video_id, reason=repr(ex)
                        ),
                    )
                seq = (seq + len(packets)) & 65535
                self.last_frame = self.frame_id
                self.event(
                    "transmitted",
                    dict(
                        frame_id=self.frame_id,
                        video_id=d.video_id,
                        tx_mono_ns=tx,
                        capture_mono_ns=capture,
                        encoded_bytes=d.encoded_bytes,
                        capture_origin="application_availability"
                        if self.source.isdecimal()
                        else "playback_release",
                        config_version=d.config_version,
                        plan_version=d.plan_version,
                        frame_type=kind,
                    ),
                )
                self.frame_id += 1
                self.gop_frame = (self.gop_frame + 1) % cfg["gop_frames"]
                if self.gop_frame == 0:
                    self.gop_id += 1
                release += round(1e9 / cfg["fps"])
                # At a new phase, use the designated boundary even if it falls before our old next release.
                with self.lock:
                    if (
                        self.pending
                        and release - round(1e9 / cfg["fps"])
                        < self.pending[0]["boundary_unix_ns"]
                        < release
                    ):
                        release = self.pending[0]["boundary_unix_ns"]
        except Exception as ex:
            self.event("sender_error", dict(reason=repr(ex)))
        finally:
            if encoder:
                encoder.close()
            if sock:
                sock.close()
            if "cap" in locals():
                cap.release()
            self.event(
                "media_end",
                dict(
                    final_frame_id=self.last_frame, video_id=self.response["video_id"]
                ),
            )
