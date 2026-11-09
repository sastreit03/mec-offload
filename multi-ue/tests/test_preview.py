import asyncio
from types import SimpleNamespace

from mec_server.preview import Preview


def test_on_demand_latest_only_shared_watchers_and_switch():
    async def exercise():
        calls = []
        engine = SimpleNamespace(ues={'u': SimpleNamespace(
            alias='pixel1', connected=True, active_video='v1')})
        preview = Preview(engine, encode=lambda rgb: calls.append(rgb) or b'jpeg')
        preview.offer('u', 'v1', 0, 'unwatched')
        assert not preview.channels and not calls
        c = preview.watch('pixel1')
        assert preview.watch('pixel1') is c
        preview.offer('u', 'v1', 1, 'old')
        preview.offer('u', 'v1', 2, 'latest')
        for _ in range(100):
            if c.jpeg: break
            await asyncio.sleep(.01)
        assert calls == ['latest'] and c.frame_id == 2
        engine.ues['u'].active_video = 'v2'
        preview.offer('u', 'v1', 3, 'stale')
        await asyncio.sleep(.22)
        assert c.jpeg is None and c.frame_id is None
        preview.offer('u', 'v2', 0, 'new video')
        await asyncio.sleep(.22)
        assert c.video_id == 'v2' and c.frame_id == 0
        await preview.unwatch('pixel1', c)
        assert c.viewers == 1 and not c.task.done()
        await preview.unwatch('pixel1', c)
        assert not preview.channels and c.task.done()
        preview.offer('u', 'v2', 1, 'unwatched again')
        assert calls == ['latest', 'new video']
    asyncio.run(exercise())


def test_encoding_failure_isolated_and_shutdown():
    async def exercise():
        engine = SimpleNamespace(ues={'u': SimpleNamespace(
            alias='pixel1', connected=True, active_video='v')})
        def fail(rgb):
            raise RuntimeError('encoding failure')
        preview = Preview(engine, fail)
        c = preview.watch('pixel1')
        preview.offer('u', 'v', 0, object())
        await asyncio.sleep(.1)
        assert c.error == 'encoding failure' and not c.task.done()
        await preview.close()
        assert c.task.done() and not preview.channels
    asyncio.run(exercise())


def test_dashboard_and_status_routes(tmp_path):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from mec_server.config import load
    from mec_server.engine import Engine
    from mec_server.app import create_app
    root = Path(__file__).resolve().parents[1]
    cfg, catalog, profiles = load(root / 'config/mec.development.yaml')
    cfg.runs_dir = str(tmp_path)
    engine = Engine(cfg, catalog, profiles)
    # No engine lifecycle or network sockets needed for these read-only routes.
    client = TestClient(create_app(engine))
    assert 'Received and decoded video' in client.get('/').text
    state = client.get('/api/status').json()
    assert state['preview']['streams'] == {}
    assert 'readiness' in state and 'ues' in state
    assert client.get('/preview.mjpg?ue_alias=missing').status_code == 404
    assert not engine.preview.channels


def test_mjpeg_subscription_cleanup():
    async def exercise():
        from pathlib import Path
        from mec_server.config import load
        from mec_server.engine import Engine
        from mec_server.app import create_app
        root = Path(__file__).resolve().parents[1]
        cfg, catalog, profiles = load(root / 'config/mec.development.yaml')
        engine = Engine(cfg, catalog, profiles)
        engine.ues['u'] = SimpleNamespace(alias='pixel1', connected=True, active_video='v')
        app = create_app(engine)
        engine.preview.encode = lambda rgb: b'jpeg-data'
        endpoint = next(r.endpoint for r in app.routes if r.path == '/preview.mjpg')
        class Request:
            async def is_disconnected(self):
                return False
        response = await endpoint(Request(), 'pixel1')
        iterator = response.body_iterator
        first = asyncio.create_task(anext(iterator))
        await asyncio.sleep(.01)
        engine.preview.offer('u', 'v', 1, object())
        packet = await asyncio.wait_for(first, 1)
        assert b'Content-Type: image/jpeg' in packet and b'jpeg-data' in packet
        assert engine.preview.channels['pixel1'].viewers == 1
        await iterator.aclose()
        assert not engine.preview.channels
    asyncio.run(exercise())
