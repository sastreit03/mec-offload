"""On-demand, latest-frame-only previews; all state is owned by the API event loop."""
import asyncio
import time
from dataclasses import dataclass
from .shared_rgb import RGBLease, release_rgb


@dataclass
class Channel:
    viewers: int = 0
    pending: object = None
    jpeg: bytes | None = None
    video_id: str | None = None
    frame_id: int | None = None
    sequence: int = 0
    error: str | None = None
    task: object = None


def encode_jpeg(rgb):
    import cv2

    height, width = rgb.shape[:2]
    if width > 960:
        rgb = cv2.resize(rgb, (960, round(height * 960 / width)))
    ok, jpeg = cv2.imencode('.jpg', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    return jpeg.tobytes()


class Preview:
    def __init__(self, engine, encode=encode_jpeg):
        self.engine = engine
        self.encode = encode
        self.channels = {}

    def video(self, alias):
        return next((u.active_video for u in self.engine.ues.values()
                     if u.connected and u.alias == alias), None)

    def watch(self, alias):
        channel = self.channels.get(alias)
        if channel is None:
            channel = self.channels[alias] = Channel()
            channel.task = asyncio.create_task(self.run(alias, channel))
        channel.viewers += 1
        return channel

    async def unwatch(self, alias, channel):
        channel.viewers -= 1
        if channel.viewers == 0:
            self.channels.pop(alias, None)
            channel.task.cancel()
            await asyncio.gather(channel.task, return_exceptions=True)
            self.release_pending(channel)
            channel.jpeg = None

    def offer(self, uid, video_id, frame_id, rgb):
        ue = self.engine.ues.get(uid)
        channel = self.channels.get(ue.alias) if ue else None
        if channel and ue.connected and ue.active_video == video_id:
            # Keep only a reference to the newest decoded image. Never mutate it.
            self.release_pending(channel)
            channel.pending = (video_id, frame_id, rgb.retain() if isinstance(rgb, RGBLease) else rgb)

    @staticmethod
    def release_pending(channel):
        if channel.pending is not None:
            release_rgb(channel.pending[2])
            channel.pending = None

    def encode_owned(self, rgb):
        try:
            return self.encode(rgb.array() if isinstance(rgb, RGBLease) else rgb)
        finally:
            release_rgb(rgb)

    async def run(self, alias, channel):
        try:
            await self._run(alias, channel)
        finally:
            self.release_pending(channel)

    async def _run(self, alias, channel):
        while True:
            started = time.monotonic()
            current = self.video(alias)
            if channel.video_id != current:
                channel.jpeg = None
                channel.video_id = current
                channel.frame_id = None
            pending, channel.pending = channel.pending, None
            if pending is not None and pending[0] == current:
                try:
                    encoding = asyncio.create_task(asyncio.to_thread(self.encode_owned, pending[2]))
                    try:
                        jpeg = await asyncio.shield(encoding)
                    except asyncio.CancelledError:
                        # Cancelling an asyncio waiter does not stop a JPEG thread.
                        # Keep its lease until encoding actually exits.
                        await asyncio.gather(encoding, return_exceptions=True)
                        raise
                    if self.video(alias) == current:
                        channel.jpeg = jpeg
                        channel.video_id, channel.frame_id = pending[:2]
                        channel.sequence += 1
                        channel.error = None
                except Exception as exc:
                    channel.error = str(exc)
            elif pending is not None:
                release_rgb(pending[2])
            await asyncio.sleep(max(0, 0.2 - (time.monotonic() - started)))

    def snapshot(self):
        return dict(max_fps=5, max_width=960, streams={alias: dict(
            viewers=c.viewers, video_id=c.video_id, frame_id=c.frame_id,
            error=c.error) for alias, c in self.channels.items()})

    async def close(self):
        tasks = [c.task for c in self.channels.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for channel in self.channels.values():
            self.release_pending(channel)
        self.channels.clear()
