from __future__ import annotations

import logging
import threading
import time

import gi
import numpy as np

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402

from models import DecodedFrame, FrameBuffer, NetworkConfig, ReceiverConfig, SharedState


LOG = logging.getLogger("mec.receiver")
Gst.init(None)


class VideoReceiver:
    """Receive RTP/H.264/UDP, decode it, preview it, and populate an RGB ring buffer."""

    def __init__(
        self,
        network: NetworkConfig,
        config: ReceiverConfig,
        state: SharedState,
        frame_buffer: FrameBuffer,
    ) -> None:
        self._network = network.model_copy(deep=True)
        self._cfg = config.model_copy(deep=True)
        self._state = state
        self._frames = frame_buffer

        self._lock = threading.RLock()
        self._pipeline: Gst.Pipeline | None = None
        self._bus_thread: threading.Thread | None = None
        self._bus_stop = threading.Event()
        self._decoded_sequence = 0

    def _pipeline_description(self) -> str:
        address = self._network.video_bind_address
        port = self._network.video_port
        pt = self._cfg.rtp_payload_type
        latency = self._cfg.jitter_latency_ms
        udp_buffer = self._cfg.udp_buffer_bytes
        quality = self._cfg.preview_jpeg_quality

        return f"""
            udpsrc name=udp address={address} port={port} buffer-size={udp_buffer}
                caps=\"application/x-rtp,media=video,encoding-name=H264,payload={pt},clock-rate=90000\" !
            rtpjitterbuffer name=jitter latency={latency} drop-on-latency=true do-lost=true !
            rtph264depay ! h264parse ! avdec_h264 !
            videoconvert ! video/x-raw,format=RGB !
            tee name=decoded

            decoded. ! queue leaky=downstream max-size-buffers=2 !
                videoconvert ! jpegenc quality={quality} !
                appsink name=preview emit-signals=true max-buffers=1 drop=true
                        sync=false wait-on-eos=false

            decoded. ! queue leaky=downstream max-size-buffers=2 !
                appsink name=ai_frames emit-signals=true max-buffers=1 drop=true
                        sync=false wait-on-eos=false
        """

    def start(self) -> None:
        with self._lock:
            if self._pipeline is not None:
                return

            desc = self._pipeline_description()
            LOG.debug("GStreamer pipeline: %s", " ".join(desc.split()))
            pipeline = Gst.parse_launch(desc)
            if not isinstance(pipeline, Gst.Pipeline):
                raise RuntimeError("GStreamer did not create a Pipeline")

            preview = pipeline.get_by_name("preview")
            ai_frames = pipeline.get_by_name("ai_frames")
            jitter = pipeline.get_by_name("jitter")
            if preview is None or ai_frames is None or jitter is None:
                pipeline.set_state(Gst.State.NULL)
                raise RuntimeError("Failed to obtain named GStreamer elements")

            preview.connect("new-sample", self._on_preview_sample)
            ai_frames.connect("new-sample", self._on_ai_sample)

            jitter_src = jitter.get_static_pad("src")
            if jitter_src is None:
                pipeline.set_state(Gst.State.NULL)
                raise RuntimeError("rtpjitterbuffer has no src pad")
            jitter_src.add_probe(Gst.PadProbeType.BUFFER, self._on_rtp_packet)

            self._bus_stop.clear()
            self._pipeline = pipeline
            result = pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                self._pipeline = None
                pipeline.set_state(Gst.State.NULL)
                raise RuntimeError("Failed to set receiver pipeline to PLAYING")

            self._state.set_receiver_running(True, None)
            self._bus_thread = threading.Thread(
                target=self._monitor_bus,
                args=(pipeline,),
                name="mec-gst-bus",
                daemon=True,
            )
            self._bus_thread.start()
            LOG.info(
                "Listening for RTP/H.264 on %s:%d (PT=%d, jitter=%d ms)",
                self._network.video_bind_address,
                self._network.video_port,
                self._cfg.rtp_payload_type,
                self._cfg.jitter_latency_ms,
            )

    def stop(self) -> None:
        with self._lock:
            pipeline = self._pipeline
            if pipeline is None:
                self._state.set_receiver_running(False)
                return
            self._pipeline = None
            self._bus_stop.set()
            pipeline.set_state(Gst.State.NULL)
            bus_thread = self._bus_thread
            self._bus_thread = None

        if bus_thread and bus_thread.is_alive() and bus_thread is not threading.current_thread():
            bus_thread.join(timeout=2.0)
        self._state.set_receiver_running(False)
        LOG.info("Video receiver stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    def _on_preview_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS

        buffer = sample.get_buffer()
        ok, map_info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            jpeg = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        pts_ns = None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts)
        self._state.set_preview(jpeg, pts_ns)
        return Gst.FlowReturn.OK

    def _on_ai_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS

        buffer = sample.get_buffer()
        caps = sample.get_caps()
        if caps is None:
            return Gst.FlowReturn.ERROR

        video_info = GstVideo.VideoInfo.new_from_caps(caps)
        width = int(video_info.width)
        height = int(video_info.height)
        stride = int(video_info.stride[0])
        if stride <= 0:
            stride = width * 3

        ok, map_info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            raw = np.frombuffer(map_info.data, dtype=np.uint8)
            needed = stride * height
            if raw.size < needed:
                LOG.warning(
                    "RGB buffer too small: got=%d expected>=%d (%dx%d stride=%d)",
                    raw.size,
                    needed,
                    width,
                    height,
                    stride,
                )
                return Gst.FlowReturn.ERROR

            rows = raw[:needed].reshape(height, stride)
            rgb = rows[:, : width * 3].reshape(height, width, 3).copy()
        finally:
            buffer.unmap(map_info)

        self._decoded_sequence += 1
        pts_ns = None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts)
        frame = DecodedFrame(
            sequence=self._decoded_sequence,
            arrival_unix_ns=time.time_ns(),
            arrival_monotonic_ns=time.monotonic_ns(),
            pts_ns=pts_ns,
            width=width,
            height=height,
            rgb=rgb,
        )
        self._frames.append(frame)
        self._state.record_decoded_frame()
        return Gst.FlowReturn.OK

    def _on_rtp_packet(
        self, _pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is None or buffer.get_size() < 12:
            return Gst.PadProbeReturn.OK

        ok, map_info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            data = map_info.data
            # RTP fixed header: V/P/X/CC, M/PT, sequence, timestamp, SSRC.
            if len(data) < 12 or (data[0] >> 6) != 2:
                return Gst.PadProbeReturn.OK
            sequence = int.from_bytes(data[2:4], "big")
            timestamp = int.from_bytes(data[4:8], "big")
            ssrc = int.from_bytes(data[8:12], "big")
            self._state.record_rtp_packet(
                byte_count=buffer.get_size(),
                sequence=sequence,
                timestamp=timestamp,
                ssrc=ssrc,
            )
        finally:
            buffer.unmap(map_info)
        return Gst.PadProbeReturn.OK

    def _release_finished_pipeline(self, pipeline: Gst.Pipeline) -> None:
        with self._lock:
            if self._pipeline is pipeline:
                self._pipeline = None
                self._bus_stop.set()
        pipeline.set_state(Gst.State.NULL)

    def _monitor_bus(self, pipeline: Gst.Pipeline) -> None:
        bus = pipeline.get_bus()
        while not self._bus_stop.is_set():
            msg = bus.timed_pop_filtered(
                250 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS,
            )
            if msg is None:
                continue

            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                text = f"{err.message}; debug={debug or ''}"
                LOG.error("GStreamer receiver error: %s", text)
                self._state.set_receiver_running(False, text)
                self._release_finished_pipeline(pipeline)
                return

            if msg.type == Gst.MessageType.EOS:
                # A live UDP receiver normally doesn't reach EOS. Treat it as a
                # stopped pipeline rather than attempting a seek/restart here.
                text = "Receiver pipeline reached unexpected EOS"
                LOG.warning(text)
                self._state.set_receiver_running(False, text)
                self._release_finished_pipeline(pipeline)
                return
