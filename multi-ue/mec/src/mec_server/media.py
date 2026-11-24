"""RTP metadata is parsed on udpsrc before jitter/depay/decode. No inference in callbacks."""

import socket
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from .shared_rgb import RGBPool, release_rgb
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
        self.diagnostics = getattr(cfg, "mode", None) == "collection"
        self.packet_evidence = OrderedDict()
        self.decode_pending = OrderedDict()
        self.sequence_frames = OrderedDict()
        self.rgb_pool = RGBPool(getattr(cfg, "shared_rgb_slots", 32),
                                getattr(cfg, "decoded_max_bytes", 8*1024*1024))

    def _decode_failure(self, fid, reason, **evidence):
        item = self.decode_pending.pop(fid, None)
        if item is None:
            return
        self.post("receiver_frame_unrecoverable", dict(
            video_id=self.video_id, frame_id=fid, reason=reason,
            observed_ns=time.monotonic_ns(), **evidence))
        self.post("frame_failure", dict(descriptor=item[0], reason=reason))

    def _jitter_drop(self, sequence, reason, details):
        with self.lock:
            fid = self.sequence_frames.get(sequence)
            forwarded = self.packet_evidence.get(("jitter_output", fid), {})
            already_forwarded = sequence in forwarded.get("sequences", ())
            self.post("jitterbuffer_drop", dict(
                video_id=self.video_id, sequence=sequence, frame_id=fid,
                reason=reason, details=details, already_forwarded=already_forwarded,
                observed_ns=time.monotonic_ns()))
            # A late duplicate does not destroy an already-forwarded packet.
            # Unknown sequence numbers are logged without guessing a frame ID.
            if fid is not None and not already_forwarded:
                self._decode_failure(fid, "jitterbuffer_drop", sequence=sequence,
                                     drop_reason=reason)

    def _decoded_frame(self, descriptor):
        if not self.diagnostics:
            return
        with self.lock:
            self.decode_pending.pop(descriptor.frame_id, None)
            if descriptor.frame_type == 1:
                # IDR recovery closes preceding decode gaps in this IDR/P-only
                # stream. Do not discard newer frames merely because a drop occurred.
                for fid in list(self.decode_pending):
                    if fid < descriptor.frame_id:
                        self._decode_failure(fid, "decode_gap_before_keyframe",
                                             recovery_frame_id=descriptor.frame_id)

    def _observe_packet(self, stage, raw, descriptor, pts):
        if not self.diagnostics:
            return
        key = (stage, descriptor.frame_id)
        now = time.monotonic_ns()
        if key not in self.packet_evidence:
            self.packet_evidence[key] = dict(
                video_id=self.video_id, stage=stage, frame_id=descriptor.frame_id,
                first_observed_ns=now, packets=0, sequences=set(),
                rtp_timestamp=int.from_bytes(raw[4:8], "big"),
            )
        item = self.packet_evidence[key]
        item.update(last_observed_ns=now, last_pts_ns=int(pts))
        item["packets"] += 1
        item["sequences"].add(int.from_bytes(raw[2:4], "big"))
        if len(self.packet_evidence) > 8192:
            self._emit_packet_evidence(self.packet_evidence.popitem(last=False)[1])

    def _emit_packet_evidence(self, item):
        item = dict(item)
        item["sequences"] = sorted(item["sequences"])
        self.post("receiver_packet_summary", item)

    def _observe_buffer(self, stage, buf):
        if not self.diagnostics:
            return
        with self.lock:
            descriptor = self.pts.get(buf.pts)
        self.post("receiver_buffer", dict(
            video_id=self.video_id, stage=stage, observed_ns=time.monotonic_ns(),
            pts_ns=int(buf.pts), dts_ns=int(buf.dts), bytes=buf.get_size(),
            flags=int(buf.get_flags()),
            matched_frame_id=descriptor.frame_id if descriptor else None,
        ))

    def _decoder_input(self, pad, info):
        self._observe_buffer("decoder_input", info.get_buffer())
        return self.Gst.PadProbeReturn.OK

    def _decoder_output(self, pad, info):
        self._observe_buffer("decoder_output", info.get_buffer())
        return self.Gst.PadProbeReturn.OK

    def start(self):
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo

        Gst.init(None)
        self.rgb_pool.allocate()
        self.Gst = Gst
        self.GstVideo = GstVideo
        self.description = (
            f"udpsrc name=udp port={self.port} buffer-size={self.cfg.udp_buffer_bytes} "
            'caps="application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96" ! '
            f"rtpjitterbuffer name=jitter latency={self.cfg.jitter_ms} drop-on-latency=true do-lost=true ! "
            "rtph264depay wait-for-keyframe=true ! h264parse ! avdec_h264 name=decoder ! videoconvert ! "
            "video/x-raw,format=RGB ! appsink name=frames emit-signals=true max-buffers=1 drop=false sync=false wait-on-eos=false enable-last-sample=false"
        )
        self.pipeline = Gst.parse_launch(self.description)
        if self.diagnostics:
            jitter = self.pipeline.get_by_name("jitter")
            jitter.set_property("post-drop-messages", True)
            jitter.set_property("drop-messages-interval", 0)
        self.pipeline.get_by_name("udp").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._packet
        )
        self.pipeline.get_by_name("jitter").get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._jitter
        )
        self.pipeline.get_by_name("frames").connect("new-sample", self._sample)
        if self.diagnostics:
            decoder = self.pipeline.get_by_name("decoder")
            decoder.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._decoder_input)
            decoder.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._decoder_output)
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
        try:
            timing["image_extract_start_ns"] = time.monotonic_ns()
            with self._mapped(buf) as raw:
                return self._packet_mapped(buf, raw)
        except (ValueError, KeyError, TypeError) as ex:
            self.post("media_error", dict(video_id=self.video_id, reason=str(ex)))
            return self.Gst.PadProbeReturn.DROP

    def _packet_mapped(self, buf, raw):
        try:
            p = parse_packet(raw)
            d = p.descriptor
            if d.video_id != self.video_id or d.media_epoch != self.epoch:
                return self.Gst.PadProbeReturn.DROP
            with self.lock:
                self._observe_packet("udp_input", raw, d, buf.pts)
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
                    if self.diagnostics:
                        self.decode_pending[d.frame_id] = (d, None)
                else:
                    a = a[0]
                if self.diagnostics:
                    sequence = int.from_bytes(raw[2:4], "big")
                    self.sequence_frames[sequence] = d.frame_id
                    self.sequence_frames.move_to_end(sequence)
                    while len(self.sequence_frames) > 8192:
                        self.sequence_frames.popitem(last=False)
                complete = a.add(p)
                received_ns = time.monotonic_ns()
                # Map the receive timestamp while handling the packet, before
                # queueing it. Export must not use a later machine's clock offset.
                received_unix_ns = received_ns + time.time_ns() - time.monotonic_ns()
                self.post(
                    "progress",
                    dict(
                        descriptor=d,
                        unique_bytes=a.unique_bytes,
                        complete=complete,
                        received_ns=received_ns,
                        received_unix_ns=received_unix_ns,
                    ),
                )
                if complete:
                    if d.frame_id in self.decode_pending:
                        self.decode_pending[d.frame_id] = (d, received_ns)
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
                if self.diagnostics and s.has_field("seqnum"):
                    self._jitter_drop(int(s.get_value("seqnum")), "considered_lost", s.to_string())
        if info.type & self.Gst.PadProbeType.BUFFER:
            buf = info.get_buffer()
            try:
                with self._mapped(buf) as raw, self.lock:
                    p = parse_packet(raw)
                    self._observe_packet("jitter_output", raw, p.descriptor, buf.pts)
                    previous = self.pts.get(buf.pts)
                    if self.diagnostics and previous and previous.frame_id != p.descriptor.frame_id:
                        self.post("receiver_pts_collision", dict(
                            video_id=self.video_id, pts_ns=int(buf.pts),
                            previous_frame_id=previous.frame_id, frame_id=p.descriptor.frame_id,
                        ))
                    self.pts[buf.pts] = p.descriptor
                    if len(self.pts) > 256:
                        self.pts.pop(next(iter(self.pts)))
            except ValueError:
                pass
        return self.Gst.PadProbeReturn.OK

    @contextmanager
    def _mapped(self, buf):
        ok, mapping = buf.map(self.Gst.MapFlags.READ)
        if not ok:
            raise ValueError("GStreamer read mapping failed")
        try:
            raw = mapping.data
            if not isinstance(raw, memoryview):
                raise ValueError("GStreamer binding must expose borrowed memoryview")
            yield raw
        finally:
            buf.unmap(mapping)

    # Copy decoded storage before returning the sample to GStreamer.
    def _sample(self, sink):
        timing = {"appsink_enter_ns": time.monotonic_ns()}
        sample = sink.emit("pull-sample")
        timing["appsink_pulled_ns"] = time.monotonic_ns()
        if sample is None:
            return self.Gst.FlowReturn.OK
        buf = sample.get_buffer()
        self._observe_buffer("appsink", buf)
        caps = sample.get_caps()
        with self.lock:
            d = self.pts.pop(buf.pts, None)
        if d is None:
            self.post(
                "media_error",
                dict(video_id=self.video_id, reason="decoded_metadata_pts_missing",
                     pts_ns=int(buf.pts), observed_ns=time.monotonic_ns()),
            )
            return self.Gst.FlowReturn.OK
        info = self.GstVideo.VideoInfo.new_from_caps(caps)
        size = buf.get_size()
        if size > self.cfg.decoded_max_bytes:
            self.post("frame_failure", dict(descriptor=d, reason="decoded_byte_budget"))
            return self.Gst.FlowReturn.OK
        import numpy as np

        lease = None
        try:
            timing["image_extract_start_ns"] = time.monotonic_ns()
            with self._mapped(buf) as raw:
                # GstVideoMeta can override caps-derived stride and offset.
                meta = self.GstVideo.buffer_get_video_meta(buf)
                stride = meta.stride[0] if meta else info.stride[0]
                offset = meta.offset[0] if meta else info.offset[0]
                rgb = np.ndarray((info.height, info.width, 3), np.uint8,
                                 buffer=raw, offset=offset, strides=(stride, 3, 1))
                timing["shared_rgb_copy_start_ns"] = time.monotonic_ns()
                lease = self.rgb_pool.copy(rgb)
                timing["shared_rgb_copy_end_ns"] = time.monotonic_ns()
                del rgb
            if lease is None:
                self.post("frame_failure", dict(descriptor=d, reason="shared_rgb_pool_exhausted"))
            else:
                self.post("decoded", dict(descriptor=d, rgb=lease, decoded_ns=time.monotonic_ns(), timing=timing))
                lease = None  # Receiver handoff now owns the lease.
        except Exception as ex:
            self.post("frame_failure", dict(descriptor=d, reason="decoded_mapping_failed"))
            self.post("media_error", dict(video_id=self.video_id, reason=repr(ex)))
        finally:
            release_rgb(lease)
        self._decoded_frame(d)
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
                self.decode_pending.pop(fid, None)
            for fid, (_, complete_ns) in list(self.decode_pending.items()):
                if complete_ns is not None and now - complete_ns >= self.cfg.decode_wait_s * 1e9:
                    self._decode_failure(fid, "decode_wait_timeout",
                                         timeout_s=self.cfg.decode_wait_s)
        if self.pipeline:
            for _ in range(256):
                msg = self.bus.pop_filtered(
                    self.Gst.MessageType.ERROR | self.Gst.MessageType.WARNING
                    | self.Gst.MessageType.ELEMENT)
                if msg is None:
                    break
                if msg.type == self.Gst.MessageType.ELEMENT:
                    structure = msg.get_structure()
                    if structure and structure.get_name() == "drop-msg":
                        self._jitter_drop(int(structure.get_value("seqnum")),
                                          str(structure.get_value("reason")),
                                          structure.to_string())
                else:
                    self.post("media_error", dict(video_id=self.video_id, reason=str(
                        msg.parse_error() if msg.type == self.Gst.MessageType.ERROR
                        else msg.parse_warning())))

    def close(self):
        self.stopped = True
        if self.pipeline:
            self.pipeline.set_state(self.Gst.State.NULL)
        with self.lock:
            for item in self.packet_evidence.values():
                self._emit_packet_evidence(item)
            self.packet_evidence.clear()
        self.rgb_pool.close()


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
