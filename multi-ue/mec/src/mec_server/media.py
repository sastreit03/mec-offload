"""RTP metadata is parsed on udpsrc before jitter/depay/decode. No inference in callbacks."""

import socket
import threading
import time
from collections import OrderedDict
from mec_contracts.rtp import parse_packet, Assembly


class Receiver:
    def __init__(self, video_id, epoch, port, cfg, post):
        self.video_id = video_id
        self.epoch = epoch
        self.port = port
        self.cfg = cfg
        self.post = post
        self.assemblies = {}
        self.done = OrderedDict()
        self.pts = {}
        self.lock = threading.RLock()
        self.pipeline = None
        self.generation = 0
        self.stopped = False
        self.sender = None

    def start(self):
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo

        Gst.init(None)
        self.Gst = Gst
        self.GstVideo = GstVideo
        self.description = (
            f"udpsrc name=udp port={self.port} buffer-size={self.cfg.udp_buffer_bytes} "
            'caps="application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96" ! '
            f"rtpjitterbuffer name=jitter latency={self.cfg.jitter_ms} drop-on-latency=true do-lost=true ! "
            "rtph264depay wait-for-keyframe=true ! h264parse ! avdec_h264 ! videoconvert ! "
            "video/x-raw,format=RGB ! appsink name=frames emit-signals=true max-buffers=1 drop=false sync=false wait-on-eos=false enable-last-sample=false"
        )
        self.pipeline = Gst.parse_launch(self.description)
        self.pipeline.get_by_name("udp").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._packet
        )
        self.pipeline.get_by_name("jitter").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._jitter
        )
        self.pipeline.get_by_name("frames").connect("new-sample", self._sample)
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("receiver pipeline setup failed")
        # Aetheredge: RTP cannot arrive until admission returns this receiver's port.
        # Accept GStreamer's asynchronous transition when PLAYING is its target.
        _, state, pending = self.pipeline.get_state(0)
        if state != Gst.State.PLAYING and pending != Gst.State.PLAYING:
            raise RuntimeError("receiver did not reach PLAYING")
        self.post(
            "pipeline",
            dict(
                video_id=self.video_id,
                description=self.description,
                gstreamer=Gst.version_string(),
                port=self.port,
                staging=dict(
                    appsink_buffers=1,
                    jitter_ms=self.cfg.jitter_ms,
                    socket_requested_bytes=self.cfg.udp_buffer_bytes,
                ),
            ),
        )
        self.bus = self.pipeline.get_bus()

    # Inspect the wire descriptor before jitter buffering, depayloading, and decoding.
    def _packet(self, pad, info):
        buf = info.get_buffer()
        raw = buf.extract_dup(0, buf.get_size())
        try:
            p = parse_packet(raw)
            d = p.descriptor
            if d.video_id != self.video_id or d.media_epoch != self.epoch:
                return self.Gst.PadProbeReturn.DROP
            with self.lock:
                source = p.ssrc
                if self.sender is not None and source != self.sender:
                    raise ValueError("SSRC change requires receiver reconciliation")
                self.sender = source
                if d.frame_id in self.done:
                    return self.Gst.PadProbeReturn.DROP
                a = self.assemblies.get(d.frame_id)
                if a is None:
                    if len(self.assemblies) >= 64:
                        raise ValueError("encoded frame assembly capacity")
                    a = Assembly(d, self.cfg.encoded_max_bytes)
                    self.assemblies[d.frame_id] = (a, time.monotonic_ns())
                    self.post(
                        "descriptor",
                        dict(descriptor=d, first_packet_ns=time.monotonic_ns()),
                    )
                else:
                    a = a[0]
                complete = a.add(p)
                self.post(
                    "progress",
                    dict(
                        descriptor=d,
                        unique_bytes=a.unique_bytes,
                        complete=complete,
                        received_ns=time.monotonic_ns(),
                    ),
                )
                if complete:
                    self.done[d.frame_id] = None
                    self.done.move_to_end(d.frame_id)
                    if len(self.done) > 8192:
                        self.done.popitem(last=False)
                    del self.assemblies[d.frame_id]
        except (ValueError, KeyError, TypeError) as ex:
            self.post("media_error", dict(video_id=self.video_id, reason=str(ex)))
            return self.Gst.PadProbeReturn.DROP
        return self.Gst.PadProbeReturn.OK

    # Carry descriptor identity into decoded output using the jitterbuffer's PTS.
    def _jitter(self, pad, info):
        if info.type & self.Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            s = event.get_structure() if event else None
            if s and s.get_name() == "GstRTPPacketLost":
                self.post(
                    "packet_loss", dict(video_id=self.video_id, details=s.to_string())
                )
        if info.type & self.Gst.PadProbeType.BUFFER:
            buf = info.get_buffer()
            try:
                p = parse_packet(buf.extract_dup(0, buf.get_size()))
                with self.lock:
                    self.pts[buf.pts] = p.descriptor
                    if len(self.pts) > 256:
                        self.pts.pop(next(iter(self.pts)))
            except ValueError:
                pass
        return self.Gst.PadProbeReturn.OK

    # Copy decoded storage before returning the sample to GStreamer.
    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        with self.lock:
            d = self.pts.pop(buf.pts, None)
        if d is None:
            self.post(
                "media_error",
                dict(video_id=self.video_id, reason="decoded_metadata_pts_missing"),
            )
            return self.Gst.FlowReturn.OK
        info = self.GstVideo.VideoInfo.new_from_caps(caps)
        size = buf.get_size()
        if size > self.cfg.decoded_max_bytes:
            self.post("frame_failure", dict(descriptor=d, reason="decoded_byte_budget"))
            return self.Gst.FlowReturn.OK
        import numpy as np

        raw = buf.extract_dup(0, size)
        rgb = (
            np.frombuffer(raw, dtype=np.uint8)
            .reshape(info.height, info.stride[0])[:, : info.width * 3]
            .reshape(info.height, info.width, 3)
            .copy()
        )
        self.post(
            "decoded", dict(descriptor=d, rgb=rgb, decoded_ns=time.monotonic_ns())
        )
        return self.Gst.FlowReturn.OK

    def tick(self):
        now = time.monotonic_ns()
        with self.lock:
            expired = [
                fid
                for fid, (_, start) in self.assemblies.items()
                if (now - start) / 1e9 > self.cfg.receive_loss_s
            ]
            for fid in expired:
                a, _ = self.assemblies.pop(fid)
                self.done[fid] = None
                while len(self.done) > 8192:
                    self.done.popitem(last=False)
                self.post(
                    "frame_failure",
                    dict(descriptor=a.descriptor, reason="transport_loss"),
                )
        if self.pipeline:
            msg = self.bus.pop_filtered(
                self.Gst.MessageType.ERROR | self.Gst.MessageType.WARNING
            )
            if msg:
                self.post(
                    "media_error",
                    dict(
                        video_id=self.video_id,
                        reason=str(
                            msg.parse_error()
                            if msg.type == self.Gst.MessageType.ERROR
                            else msg.parse_warning()
                        ),
                    ),
                )

    def close(self):
        self.stopped = True
        if self.pipeline:
            self.pipeline.set_state(self.Gst.State.NULL)


class MockReceiver:
    """Control/integration tests only; never accepts real media or experimental readiness."""

    def __init__(self, *args):
        self.port = args[2]

    def start(self):
        pass

    def tick(self):
        pass

    def close(self):
        pass
