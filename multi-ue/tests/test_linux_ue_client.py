import asyncio

from mec_ue.client import Client


class Socket:
    def __init__(self):
        self.closed = None

    async def close(self, **values):
        self.closed = values


def test_sender_error_stops_client_for_supervised_restart():
    client = Client.__new__(Client)
    client.record = lambda *_args, **_values: None
    client.fatal_media_error = None
    client.stopped = False
    client.ws = Socket()

    asyncio.run(client.on_media("sender_error", {"reason": "encoder timed out"}))

    assert client.stopped is True
    assert client.fatal_media_error == "encoder timed out"
    assert client.ws.closed == {"code": 1011, "reason": "media sender failed"}
