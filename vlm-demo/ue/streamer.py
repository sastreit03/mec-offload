from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from models import NetworkConfig, SharedState, VideoConfig


LOG = logging.getLogger("ue.streamer")
Gst.init(None)


def _gst_quote(value: str) -> str:
    """Quote a value for gst_parse_launch syntax."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class VideoStreamer:
    def __init__(
        self,
        video_config: VideoConfig,
        network_config: NetworkConfig,
        state: SharedState,
        ue_ip: str,
    ) -> None:
        self._cfg = video_config.model_copy(deep=True)
        self._network = network_config.model_copy(deep=True)
        self._state = state
        self._lock = threading.RLock()
        self._pipeline: Gst.Pipeline | None = None
        self._bus_thread: threading.Thread | None = None
        self._bus_stop = threading.Event()
        self._ue_ip = ue_ip

    def get_config(self) -> VideoConfig:
        with self._lock:
            return self._cfg.model_copy(deep=True)

    def _pipeline_description(self, cfg: VideoConfig) -> str:
        source = str(Path(cfg.source).resolve())
        if not Path(source).is_file():
            raise FileNotFoundError(f"Video file does not exist: {source}")

        # Important: udpsink sync=true. Because the source is a file, this makes
        # GStreamer transmit according to the media clock rather than as fast as
        # the CPU can decode the file.
        return f"""
            filesrc location={_gst_quote(source)} !
            decodebin name=dec
            dec. ! queue ! videoconvert ! videorate !
            video/x-raw,framerate={cfg.fps}/1 !
            videocrop left={cfg.crop_left} right={cfg.crop_right}
                      top={cfg.crop_top} bottom={cfg.crop_bottom} !
            videoscale ! videoconvert !
            video/x-raw,format=I420,width={cfg.width},height={cfg.height},pixel-aspect-ratio=1/1 !
            x264enc name=enc
                    tune=zerolatency
                    speed-preset=veryfast
                    bitrate={cfg.bitrate_kbps}
                    key-int-max={cfg.gop_frames}
                    bframes=0
                    byte-stream=true !
            video/x-h264,profile=main,stream-format=byte-stream,alignment=au !
            h264parse config-interval=-1 !
            tee name=encoded

            encoded. ! queue max-size-time=1000000000 max-size-bytes=0 max-size-buffers=0 !
                rtph264pay name=pay pt=96 config-interval=-1 mtu={cfg.rtp_mtu} !
                udpsink host={self._network.mec_ip} port={self._network.video_port}
                        bind-address={self._ue_ip} bind-port=0
                        sync=true async=false

            encoded. ! queue leaky=downstream max-size-buffers=2 !
                avdec_h264 ! videoconvert !
                jpegenc quality={cfg.preview_jpeg_quality} !
                appsink name=preview emit-signals=true max-buffers=1 drop=true
                        sync=true async=false wait-on-eos=false
        """

    def start(self) -> None:
        with self._lock:
            if self._pipeline is not None:
                return

            cfg = self._cfg.model_copy(deep=True)
            desc = self._pipeline_description(cfg)
            LOG.debug("GStreamer pipeline: %s", " ".join(desc.split()))

            pipeline = Gst.parse_launch(desc)
            if not isinstance(pipeline, Gst.Pipeline):
                raise RuntimeError("GStreamer did not create a Pipeline")

            preview = pipeline.get_by_name("preview")
            pay = pipeline.get_by_name("pay")
            if preview is None or pay is None:
                pipeline.set_state(Gst.State.NULL)
                raise RuntimeError("Failed to obtain named GStreamer elements")

            preview.connect("new-sample", self._on_preview_sample)

            pay_src = pay.get_static_pad("src")
            if pay_src is None:
                pipeline.set_state(Gst.State.NULL)
                raise RuntimeError("rtph264pay has no src pad")
            pay_src.add_probe(Gst.PadProbeType.BUFFER, self._on_rtp_packet)

            self._bus_stop.clear()
            self._pipeline = pipeline

            result = pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                self._pipeline = None
                pipeline.set_state(Gst.State.NULL)
                raise RuntimeError("Failed to set GStreamer pipeline to PLAYING")

            self._state.set_stream_running(True, None)
            self._bus_thread = threading.Thread(
                target=self._monitor_bus,
                args=(pipeline,),
                name="gst-bus",
                daemon=True,
            )
            self._bus_thread.start()
            LOG.info(
                "Streaming %s at %dfps, %dx%d, %dkbps, GOP=%d to %s:%d",
                cfg.source,
                cfg.fps,
                cfg.width,
                cfg.height,
                cfg.bitrate_kbps,
                cfg.gop_frames,
                self._network.mec_ip,
                self._network.video_port,
            )

    def stop(self) -> None:
        with self._lock:
            pipeline = self._pipeline
            if pipeline is None:
                self._state.set_stream_running(False)
                return
            self._pipeline = None
            self._bus_stop.set()
            pipeline.set_state(Gst.State.NULL)
            bus_thread = self._bus_thread
            self._bus_thread = None

        if bus_thread and bus_thread.is_alive() and bus_thread is not threading.current_thread():
            bus_thread.join(timeout=2.0)
        self._state.set_stream_running(False)
        LOG.info("Video stream stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    def reconfigure(self, new_config: VideoConfig) -> None:
        # Rebuilding the media pipeline is intentionally used for FPS/GOP/resize
        # changes. It is deterministic and avoids complex in-place caps renegotiation.
        self.stop()
        with self._lock:
            self._cfg = new_config.model_copy(deep=True)
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

    def _on_rtp_packet(
        self,
        _pad: Gst.Pad,
        info: Gst.PadProbeInfo,
    ) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is not None:
            # This counts bytes at the RTP layer before UDP/IP/GTP/NR overhead.
            self._state.record_rtp_packet(buffer.get_size())
        return Gst.PadProbeReturn.OK

    def _release_finished_pipeline(self, pipeline: Gst.Pipeline) -> None:
        """Detach a pipeline that reached EOS/error without joining its own bus thread."""
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
                LOG.error("GStreamer error: %s", text)
                self._state.set_stream_error(text)
                self._state.set_stream_running(False, text)
                self._release_finished_pipeline(pipeline)
                return

            if msg.type == Gst.MessageType.EOS:
                cfg = self.get_config()
                if cfg.loop_video:
                    LOG.info("Reached EOS; restarting video pipeline")
                    self.stop()
                    self.start()
                    return
                else:
                    LOG.info("Reached end of prerecorded video")
                    self._state.set_stream_running(False)
                    self._release_finished_pipeline(pipeline)
                    return
