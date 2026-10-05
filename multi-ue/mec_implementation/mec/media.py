"""Per-task UDP reception. CPU decoding never waits for model inference."""
import io
import secrets
import socket
import threading
import time
from collections import OrderedDict

import numpy as np
from PIL import Image
from .frames import Frame
from .records import stamp
from .rtp import FRAGMENT, parse


class Receiver:
    def __init__(self, task_id, app, settings, frame_queue, log):
        self.task_id, self.app, self.cfg = task_id, app, settings
        self.frames, self.log = frame_queue, log
        self.token = secrets.token_bytes(16)
        self.socket = None
        self.stopping = threading.Event()
        self.thread = None
        self.first_media = None
        self.decoder = None
        self.partial = OrderedDict()
        self.ssrc = None
        self.port = None
        self.error = None

    def start(self, network, media_config):
        # Pipeline validation and socket binding finish before acceptance.
        if self.app.codec == "h264":
            self.decoder = GstDecoder(self.app, media_config, self.frames, self.log, self.task_id)
        for port in range(network.media_port_first, network.media_port_last + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, media_config.socket_receive_bytes)
                sock.bind((network.media_bind_host, port))
                sock.settimeout(0.02)
                self.socket, self.port = sock, port
                break
            except OSError:
                sock.close()
        if self.socket is None:
            if self.decoder:
                self.decoder.stop()
            raise RuntimeError("No media port available")
        self.log.event("media_settings", task_id=self.task_id, codec=self.app.codec,
                       socket_receive_bytes=self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
                       assembly_max_frames=media_config.max_partial_frames,
                       assembly_max_bytes=media_config.max_assembly_bytes,
                       max_encoded_frame_bytes=media_config.max_frame_bytes,
                       application_frame_cap=self.frames.cap)
        self.thread = threading.Thread(target=self._receive, name=f"rtp-{port}", daemon=True)
        self.thread.start()
        return {"transport": "rtp_udp", "host": network.advertise_host, "port": port,
                "codec": self.app.codec, "payload_type": 96 if self.app.codec == "h264" else 97,
                "media_token": self.token.hex(), "ssrc_policy": "first_valid_packet",
                "metadata": {"profile": 4096, "extension_id": 1, "version": 1, "size_bytes": 57},
                "mtu": media_config.mtu}

    def _receive(self):
        while not self.stopping.is_set():
            try:
                data, _peer = self.socket.recvfrom(65535)
                arrival = stamp()
                header, payload, _seq, rtp_time, ssrc, marker, pt = parse(data)
                if header.task_id != self.task_id or not secrets.compare_digest(header.token, self.token):
                    continue  # Old packets cannot enter a task after port reuse.
                if pt != (96 if self.app.codec == "h264" else 97):
                    continue
                if self.ssrc is None:
                    self.ssrc = ssrc
                if ssrc != self.ssrc:
                    continue
                if self.decoder:
                    if not payload or not 1 <= payload[0] & 31 <= 28:
                        raise ValueError("Invalid H.264 RTP payload")
                    self.decoder.push(data, header, rtp_time, marker, arrival)
                else:
                    self._jpeg(header, payload, arrival)
                if self.first_media is None:
                    self.first_media = arrival
                    self.log.event("first_media", task_id=self.task_id, arrival=arrival)
            except socket.timeout:
                pass
            except (ValueError, OSError) as exc:
                if not self.stopping.is_set():
                    self.log.event("media_packet_error", task_id=self.task_id, detail=str(exc))
            except RuntimeError as exc:
                self.error = repr(exc)
                self.log.event("media_failed", task_id=self.task_id, detail=self.error)
                break
            except Exception as exc:
                self.log.event("media_decode_error", task_id=self.task_id, detail=repr(exc))
            self._expire()

    def _jpeg(self, header, payload, arrival):
        if len(payload) < FRAGMENT.size:
            raise ValueError("Truncated JPEG fragment")
        offset, total = FRAGMENT.unpack_from(payload)
        part = payload[FRAGMENT.size:]
        if not total or total > self.cfg.max_frame_bytes or offset + len(part) > total:
            raise ValueError("Invalid JPEG fragment bounds")
        item = self.partial.get(header.frame_id)
        if item is None:
            while len(self.partial) >= self.cfg.max_partial_frames:
                fid, old = self.partial.popitem(last=False)
                self.log.event("assembly_overflow", task_id=self.task_id, frame_id=fid, loss_detected_at=stamp())
            item = {"header": header, "total": total, "parts": {}, "first": arrival, "bytes": 0}
            self.partial[header.frame_id] = item
        if item["header"] != header or item["total"] != total:
            raise ValueError("Inconsistent frame metadata")
        # Nonoverlapping fragments prevent malformed senders faking completeness.
        for old_offset, old_part in item["parts"].items():
            if offset == old_offset and part == old_part:
                return
            if offset < old_offset + len(old_part) and old_offset < offset + len(part):
                raise ValueError("Overlapping JPEG fragments")
        item["parts"][offset] = part
        item["bytes"] += len(part)
        while sum(i["bytes"] for i in self.partial.values()) > self.cfg.max_assembly_bytes:
            fid, _old = self.partial.popitem(last=False)
            self.log.event("assembly_byte_overflow", task_id=self.task_id, frame_id=fid, loss_detected_at=stamp())
            if fid == header.frame_id:
                return
        if item["bytes"] != total:
            return
        del self.partial[header.frame_id]
        ordered = sorted(item["parts"].items())
        cursor = 0
        for offset, part in ordered:
            if offset != cursor:
                raise ValueError("JPEG fragment gap")
            cursor += len(part)
        encoded = b"".join(part for _, part in ordered)
        with Image.open(io.BytesIO(encoded)) as image:
            if image.size != (self.app.width, self.app.height):
                raise ValueError("Frame dimensions do not match admitted configuration")
            rgb = np.array(image.convert("RGB"))
        times = {"frame_first_packet_receive": item["first"],
                 "frame_complete_receive": arrival, "decode_finish": stamp()}
        self.frames.put(Frame(header.frame_id, rgb, header.capture_unix_ns, header.tx_unix_ns, times))

    def _expire(self):
        now = time.monotonic_ns()
        expired = [fid for fid, item in self.partial.items()
                   if now - item["first"]["mono_ns"] > self.cfg.assembly_timeout_s * 1e9]
        for fid in expired:
            del self.partial[fid]
            self.log.event("incomplete_frame", task_id=self.task_id, frame_id=fid, loss_detected_at=stamp())

    def stop(self):
        self.stopping.set()
        if self.socket:
            self.socket.close()
        if self.decoder:
            self.decoder.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.partial.clear()


class GstDecoder:
    """Real RTP/H.264 pipeline; map RTP timestamp -> PTS -> frame metadata."""
    def __init__(self, app, cfg, frames, log, task_id):
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo
        Gst.init(None)
        self.Gst, self.GstVideo = Gst, GstVideo
        self.app, self.cfg, self.frames, self.log, self.task_id = app, cfg, frames, log, task_id
        self.lock = threading.RLock()
        self.metadata = OrderedDict()
        self.by_pts = OrderedDict()
        self.description = (
            f'appsrc name=input is-live=true format=time block=true max-buffers={cfg.appsrc_max_buffers} '
            'max-bytes=0 max-time=0 caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" ! '
            f'rtpjitterbuffer name=jitter latency={cfg.jitter_latency_ms} drop-on-latency=true do-lost=true ! '
            'rtph264depay name=depay ! h264parse ! avdec_h264 ! videoconvert ! video/x-raw,format=RGB ! '
            'appsink name=frames emit-signals=true max-buffers=1 drop=false sync=false '
            'wait-on-eos=false enable-last-sample=false')
        self.pipeline = Gst.parse_launch(self.description)
        self.source = self.pipeline.get_by_name("input")
        sink = self.pipeline.get_by_name("frames")
        jitter = self.pipeline.get_by_name("jitter")
        if jitter.find_property("post-drop-messages"):
            jitter.set_property("post-drop-messages", True)
        # Version-specific defaults are explicitly disabled when present.
        for elem in (self.source, sink):
            for name in ("max-bytes", "max-time"):
                if elem.find_property(name):
                    elem.set_property(name, 0)
            if elem.find_property("leaky-type"):
                elem.set_property("leaky-type", 0)
        sink.connect("new-sample", self._sample)
        depay = self.pipeline.get_by_name("depay")
        if depay and depay.find_property("wait-for-keyframe"):
            depay.set_property("wait-for-keyframe", True)
        jitter.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._rtp_probe)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("GStreamer pipeline could not start")
        effective = {}
        for elem, names in ((self.source, ["block", "max-buffers", "max-bytes", "max-time", "leaky-type"]),
                            (sink, ["max-buffers", "drop", "sync", "wait-on-eos", "enable-last-sample", "max-bytes", "max-time", "leaky-type"]),
                            (jitter, ["latency", "drop-on-latency", "do-lost", "post-drop-messages"]),
                            (depay, ["wait-for-keyframe"])):
            effective[elem.name] = {n: str(elem.get_property(n)) for n in names if elem.find_property(n)}
        log.event("gstreamer_settings", task_id=task_id, version=Gst.version_string(),
                  pipeline=self.description, effective=effective, optional_decoded_queue=False)

    def push(self, data, header, rtp_time, marker, arrival):
        with self.lock:
            key = (rtp_time, header.frame_id)
            entry = self.metadata.get(key)
            if entry is None:
                entry = {"header": header, "times": {"frame_first_packet_receive": arrival}}
                self.metadata[key] = entry
            elif entry["header"] != header:
                raise ValueError("Inconsistent H.264 metadata")
            if marker:
                entry["times"]["rtp_marker_receive"] = arrival
            entry["times"]["frame_last_packet_receive"] = arrival
            while len(self.metadata) > self.cfg.max_partial_frames:
                _, old = self.metadata.popitem(last=False)
                self.log.event("decoder_metadata_evicted", task_id=self.task_id, frame_id=old["header"].frame_id)
        buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        if self.source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
            raise RuntimeError("GStreamer rejected RTP input")
        bus = self.pipeline.get_bus()
        while True:
            msg = bus.pop_filtered(self.Gst.MessageType.ERROR | self.Gst.MessageType.ELEMENT)
            if msg is None:
                break
            if msg.type == self.Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                raise RuntimeError(f"GStreamer: {err.message}: {debug}")
            self.log.event("gstreamer_message", task_id=self.task_id, message=str(msg.get_structure()))

    def _rtp_probe(self, _pad, info):
        if info.type & self.Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            structure = event.get_structure() if event else None
            if structure and structure.get_name() == "GstRTPPacketLost":
                self.log.event("rtp_packet_lost", task_id=self.task_id, detail=structure.to_string(), loss_detected_at=stamp())
            return self.Gst.PadProbeReturn.OK
        buffer = info.get_buffer()
        if buffer:
            data = buffer.extract_dup(0, buffer.get_size())
            try:
                header, _, _, rtp_time, _, _, _ = parse(data)
                with self.lock:
                    item = self.metadata.get((rtp_time, header.frame_id))
                    if item and buffer.pts != self.Gst.CLOCK_TIME_NONE:
                        self.by_pts[int(buffer.pts)] = item
                        while len(self.by_pts) > self.cfg.max_partial_frames:
                            self.by_pts.popitem(last=False)
            except ValueError:
                pass
        return self.Gst.PadProbeReturn.OK

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        with self.lock:
            item = self.by_pts.pop(int(buffer.pts), None)
        if item is None:
            self.log.event("frame_metadata_missing", task_id=self.task_id, pts_ns=int(buffer.pts))
            return self.Gst.FlowReturn.OK  # Never invent frame identity.
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        if (info.width, info.height) != (self.app.width, self.app.height):
            self.log.event("unexpected_frame_dimensions", task_id=self.task_id)
            return self.Gst.FlowReturn.OK
        raw = buffer.extract_dup(0, buffer.get_size())
        stride = info.stride[0]
        rgb = np.frombuffer(raw, np.uint8).reshape(info.height, stride)[:, :info.width * 3].reshape(info.height, info.width, 3).copy()
        header = item["header"]
        with self.lock:
            for key in list(self.metadata):
                if key[1] == header.frame_id:
                    del self.metadata[key]
        # H.264 completion means the last observed packet belonging to the
        # decoded access unit, not proof that every transmitted packet arrived.
        times = {**item["times"], "frame_complete_receive": item["times"]["frame_last_packet_receive"], "decode_finish": stamp()}
        self.frames.put(Frame(header.frame_id, rgb, header.capture_unix_ns, header.tx_unix_ns, times))
        return self.Gst.FlowReturn.OK

    def stop(self):
        self.pipeline.set_state(self.Gst.State.NULL)
