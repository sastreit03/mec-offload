"""CPU control-flow tests; these are not evidence of hardware overlap."""
import queue
from threading import Event, Thread
from types import SimpleNamespace
import numpy as np
from mec_server import workers


def test_observer_releases_rgb_after_read_event_without_waiting_for_inference(monkeypatch):
    read_done, inference_done, launched_two = Event(), Event(), Event()
    mappings = []
    class Mapping:
        def __init__(self):
            self.image = np.zeros((2,2,3),np.uint8)
            self.closed = False
            mappings.append(self)
        def close(self):
            assert read_done.is_set()
            self.closed = True
    class Adapter:
        cuda_graphs = True
        def __init__(self,*args,**kwargs):
            self.rgb_cache=SimpleNamespace(acquire=lambda wire: Mapping())
            self.torch=SimpleNamespace(cuda=SimpleNamespace(nvtx=SimpleNamespace(
                range_push=lambda *a: None,range_pop=lambda: None)))
        def launch(self,job):
            if job['job_id'][1]==1:
                assert not inference_done.is_set() and not read_done.is_set()
                launched_two.set()
            return SimpleNamespace(ready=SimpleNamespace(query=inference_done.is_set),
                input_read_done=SimpleNamespace(query=read_done.is_set),
                input_mapping=job['_cuda_input'],times={})
        def finish(self,job,submission):
            assert inference_done.is_set()
            return {},submission.times
    monkeypatch.setattr(workers,'TorchAdapter',Adapter)
    incoming,outgoing=queue.Queue(),queue.Queue()
    for fid in (0,1):
        incoming.put(dict(job_id=['v',fid],priority=0,shared_rgb={'generation':fid+1}))
    incoming.put(None)
    thread=Thread(target=workers.worker_main,args=('detection',{},dict(backend='torchscript',
        weights_dir='',priority_streams=[0],warmup_count=0),incoming,outgoing))
    thread.start()
    try:
        assert outgoing.get(timeout=5)['type']=='ready'
        assert launched_two.wait(5)
        assert not any(m.closed for m in mappings)
        assert outgoing.empty()
        read_done.set()
        releases=[outgoing.get(timeout=5),outgoing.get(timeout=5)]
        assert all(m['type']=='input_released' for m in releases)
        assert all(m.closed for m in mappings) and not inference_done.is_set()
        inference_done.set()
        results=[outgoing.get(timeout=5),outgoing.get(timeout=5)]
        assert all(m['type']=='completed' for m in results)
        assert sorted(m['job_id'][1] for m in results)==[0,1]
    finally:
        read_done.set(); inference_done.set()
        thread.join(5)
    assert not thread.is_alive()
