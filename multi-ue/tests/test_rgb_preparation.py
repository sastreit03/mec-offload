"""CPU tests for preparation barriers, cancellation and registered-region ownership."""
import asyncio
import ctypes
import queue
from types import SimpleNamespace
import numpy as np
import pytest
from mec_server.cuda_rgb import RegisteredRGB
from mec_server.shared_rgb import RGBPool
from mec_server.workers import Workers
from test_control_freshness import engine


class Native:
    def __init__(self):
        self.registrations = 0
        self.unregistrations = 0
        self.lib = self
    def register_region(self, address, size):
        self.registrations += 1
        return 0
    def device_pointer(self, address, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = address
        return 0
    def unregister_region(self, address):
        self.unregistrations += 1
        return 0
    def check(self, code):
        assert code == 0


def test_prepared_empty_pool_is_pinned_until_release_and_last_reader():
    native = Native()
    cache = RegisteredRGB(native, capacity=1)
    first, second = RGBPool(1, 12), RGBPool(1, 12)
    try:
        cache.prepare(first.registration())
        assert first.stats()['copies'] == 0
        assert native.registrations == 1
        with pytest.raises(RuntimeError, match='capacity'):
            cache.prepare(second.registration())
        lease = first.copy(np.ones((2,2,3), np.uint8))
        reader = cache.acquire(lease.wire())
        assert native.registrations == 1  # First frame never registers again.
        cache.release(first.region.name)
        assert native.unregistrations == 0  # GPU reader still owns the mapping.
        reader.close()
        assert native.unregistrations == 1
        lease.release()
        cache.prepare(second.registration())
        cache.release(second.region.name)
        assert not cache.entries
    finally:
        cache.close()
        first.close(); second.close()


def control_workers():
    workers = Workers({'configurations':[dict(application='detection',model=str(i)) for i in range(4)]},
                      dict(backend='torchscript', applications=['detection'], priority_streams=[0]))
    workers.inputs = {'detection':queue.Queue()}
    workers.output = queue.Queue()
    workers.processes = {'detection':SimpleNamespace(is_alive=lambda: True, exitcode=None)}
    workers.ready = {'detection':dict(streams=[0])}
    return workers


@pytest.mark.parametrize('outcome', ['ready','failure','timeout','cancel','exit'])
def test_preparation_acknowledgement_and_cleanup(outcome):
    async def exercise():
        workers = control_workers()
        pool = RGBPool(1,12)
        try:
            task = asyncio.create_task(workers.prepare_rgb('detection',pool,timeout=.05))
            await asyncio.sleep(0)
            command = workers.inputs['detection'].get_nowait()
            assert command['command'] == 'prepare_rgb' and not task.done()
            if outcome in ('ready','failure'):
                workers.output.put(dict(type='rgb_prepared' if outcome=='ready' else 'rgb_prepare_failed',
                    application='detection', region=pool.region.name, error='registration failed', registration_ms=1))
                list(workers.poll())
            elif outcome == 'cancel':
                task.cancel()
            elif outcome == 'exit':
                workers.processes['detection'].exitcode=1
                list(workers.poll())
            if outcome == 'ready':
                assert (await task)['region'] == pool.region.name
                assert workers.inputs['detection'].empty()
            else:
                expected=asyncio.CancelledError if outcome=='cancel' else TimeoutError if outcome=='timeout' else RuntimeError
                with pytest.raises(expected):
                    await task
                assert workers.inputs['detection'].get_nowait()['command']=='release_rgb'
            assert not workers.rgb_preparations
        finally:
            pool.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['ready','failure','aborted','cancel'])
def test_admission_waits_for_registration_without_blocking_control(engine, monkeypatch, outcome):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        receivers, released = [], []
        class Receiver:
            def __init__(self,*args):
                self.rgb_pool=RGBPool(1,12)
                receivers.append(self)
            def start(self): self.rgb_pool.allocate()
            def close(self): self.rgb_pool.close()
        async def prepare(app,pool):
            entered.set()
            await release.wait()
            if outcome=='failure': raise RuntimeError('registration failed')
            return dict(region=pool.region.name,registration_ms=1)
        engine.cfg.backend='torchscript'
        monkeypatch.setattr('mec_server.engine.Receiver',Receiver)
        monkeypatch.setattr(engine.workers,'prepare_rgb',prepare)
        monkeypatch.setattr(engine.workers,'release_rgb',lambda app,region: released.append(region))
        monkeypatch.setattr(engine,'readiness',lambda: {'ready':True})
        monkeypatch.setattr(engine,'usable_radio',lambda *a,**kw: True)
        user,_=engine.hello(None,'12.1.1.3',lambda _:None)
        task=asyncio.create_task(engine.start_video(user,dict(client_seq=1,application='detection',
            input=dict(width=1280,height=720,fps=engine.cfg.fps))))
        await asyncio.wait_for(entered.wait(),2)
        assert not task.done()
        assert (await engine.command(user,dict(type='time_sync',client_mono_ns=1)))['type']=='time_sync'
        if outcome=='aborted':
            engine.cfg.mode='collection'
            engine.scenario_error={'reason':'operator_abort'}
        if outcome=='cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            release.set()
            result=await task
            assert result['accepted'] is (outcome=='ready')
            video=engine.videos[result['video_id']]
            if outcome=='ready':
                assert video.rgb_region == receivers[0].rgb_pool.region.name
                engine.release_receiver_rgb(video)
                receivers[0].close()
            if outcome in ('ready','aborted'):
                assert len(released)==1
        assert receivers[0].rgb_pool.closed
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', [False, True])
def test_successor_waits_for_its_own_region_registration(engine, monkeypatch, failure):
    import time
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        receivers, registered, retired = [], [], []
        class Receiver:
            def __init__(self,*args):
                self.rgb_pool=RGBPool(1,12)
                receivers.append(self)
            def start(self): self.rgb_pool.allocate()
            def close(self): self.rgb_pool.close()
        async def prepare(app,pool):
            if registered:
                entered.set()
                await release.wait()
                if failure: raise RuntimeError('successor registration failed')
            registered.append(pool.region.name)
            return dict(region=pool.region.name,registration_ms=1)
        engine.cfg.backend='torchscript'
        monkeypatch.setattr('mec_server.engine.Receiver',Receiver)
        monkeypatch.setattr(engine.workers,'prepare_rgb',prepare)
        monkeypatch.setattr(engine.workers,'release_rgb',lambda app,region: retired.append(region))
        monkeypatch.setattr(engine,'readiness',lambda: {'ready':True})
        monkeypatch.setattr(engine,'usable_radio',lambda *a,**kw: True)
        user,_=engine.hello(None,'12.1.1.3',lambda _:None)
        request=dict(application='detection',input=dict(width=1280,height=720,fps=engine.cfg.fps))
        first=await engine.start_video(user,dict(request,client_seq=1))
        assert first['accepted']
        second=asyncio.create_task(engine.start_video(user,dict(request,client_seq=2,
            predecessor_video_id=first['video_id'],expected_start_unix_ns=time.time_ns()+10**9)))
        try:
            await asyncio.wait_for(entered.wait(),2)
            assert not second.done() and user.prepared_video is None
            release.set()
            result=await second
            assert result['accepted'] is not failure
            if not failure:
                assert user.prepared_video == result['video_id']
                assert len(set(registered)) == 2
                engine.release_receiver_rgb(engine.videos[result['video_id']])
            else:
                assert receivers[1].rgb_pool.closed
        finally:
            engine.release_receiver_rgb(engine.videos[first['video_id']])
            for receiver in receivers: receiver.close()
        assert sorted(retired)==sorted(registered)
    asyncio.run(exercise())
