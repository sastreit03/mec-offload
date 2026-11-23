"""Opt-in real CUDA tests of mapped preprocessing, graphs and slot lifetime."""
import os
from pathlib import Path
import numpy as np
import pytest
from mec_server.shared_rgb import RGBPool

pytestmark = pytest.mark.skipif(os.getenv('MEC_TEST_CUDA') != '1', reason='requires Spark CUDA image')


def reference(image, hw, segmentation):
    import torch
    import torch.nn.functional as F
    h, w = image.shape[:2]
    oh, ow = hw
    scale = min(oh/h, ow/w)
    nh, nw = round(h*scale), round(w*scale)
    top, left = (oh-nh)//2, (ow-nw)//2
    x = torch.from_numpy(image).cuda().permute(2,0,1).unsqueeze(0).half()/255
    x = F.interpolate(x, size=(nh,nw), mode='bilinear', align_corners=False)
    x = F.pad(x, (left,ow-nw-left,top,oh-nh-top), value=114/255)
    if segmentation:
        mean = torch.tensor([.485,.456,.406], device='cuda', dtype=torch.float16).view(1,3,1,1)
        std = torch.tensor([.229,.224,.225], device='cuda', dtype=torch.float16).view(1,3,1,1)
        x = (x-mean)/std
    return x


@pytest.mark.parametrize('source,hw,segmentation', [
    ((720,1280),(640,640),False), ((720,1280),(512,896),True),
    ((31,57),(32,48),False), ((31,57),(48,64),True)])
def test_mapped_preprocessing_matches_existing_fp16(source,hw,segmentation):
    import torch
    from mec_server.cuda_rgb import NativeRGB, RegisteredRGB
    native = NativeRGB()
    native.check(native.lib.mapping_flags())
    cache = RegisteredRGB(native)
    pool = RGBPool(1, source[0]*source[1]*3)
    image = np.random.default_rng(42).integers(0,256,(*source,3),dtype=np.uint8)
    lease = pool.copy(image)
    mapped = cache.acquire(lease.wire())
    try:
        expected = reference(image, hw, segmentation)
        x = torch.empty_like(expected, memory_format=torch.contiguous_format)
        h,w = source
        oh,ow = hw
        scale = min(oh/h,ow/w)
        nh,nw = round(h*scale),round(w*scale)
        native.check(native.lib.preprocess(mapped.pointer,x.data_ptr(),w,h,mapped.descriptor.row_stride,
            ow,oh,nw,nh,(ow-nw)//2,(oh-nh)//2,int(segmentation),torch.cuda.current_stream().cuda_stream))
        torch.cuda.synchronize()
        diff = (x-expected).abs()
        assert torch.equal(x,expected), dict(max_error=diff.max().item(), different=(x!=expected).sum().item(),
                                             layout=expected.stride())
    finally:
        torch.cuda.synchronize()
        mapped.close()
        lease.release()
        cache.close()
        pool.close()


def test_graph_private_storage_output_preservation_and_slot_reuse(tmp_path):
    import torch
    from mec_server.workers import TorchAdapter
    class Boxes(torch.nn.Module):
        def forward(self,x):
            score=x.mean().float()
            return torch.stack([score*0,score*0,score,score,score,score*0]).reshape(1,1,6)
    torch.jit.trace(Boxes(),torch.ones(1,3,32,32)).save(str(tmp_path/'boxes.pt'))
    spec=dict(model='tiny',artifact='boxes.pt',application='detection',input_hw=[32,32],confidence=0)
    specs={'tiny':spec,'other':dict(spec,model='other')}
    adapter=TorchAdapter(specs,tmp_path,[-3,0],False,cuda_graphs=True)
    for name,model in list(adapter.models.items()):
        def delayed(x,model=model):
            torch.cuda._sleep(100_000_000)
            return model(x)
        adapter.models[name]=delayed
    adapter.prepare_graphs(specs)
    assert len({state[1].data_ptr() for state in adapter.graphs.values()})==4
    pool=RGBPool(2,32*32*3)
    pending=[]
    early_releases=0
    try:
        for fid,value in enumerate([0,255,37,128,9,230]):
            image=np.full((32,32,3),value,np.uint8)
            lease=pool.copy(image)
            assert lease is not None
            mapped=adapter.rgb_cache.acquire(lease.wire())
            priority=(-3,0)[fid%2]
            configuration=specs[("tiny","other")[(fid//2)%2]]
            job=dict(configuration=configuration,priority=priority,rgb=mapped.image,_cuda_input=mapped)
            submission=adapter.launch(job)
            # Release only at the actual last-read event, then reuse the slot.
            submission.input_read_done.synchronize()
            early_releases += not submission.ready.query()
            job.pop('rgb'); job.pop('_cuda_input')
            mapped.close(); lease.release(); submission.input_mapping=None
            pending.append((job,submission,value))
        for job,submission,value in pending:
            payload,_=adapter.finish(job,submission)
            expected=float(torch.tensor(value/255,dtype=torch.float16))
            assert payload['detections'][0]['score']==pytest.approx(expected,abs=1e-5)
            assert submission.times['cuda_graph'] and submission.times['shared_rgb_direct']
        assert early_releases > 0
        assert pool.stats()['in_use']==0
    finally:
        adapter.close()
        pool.close()


def test_exported_cpu_batch_index_relocated_without_changing_output():
    import torch
    from mec_server.workers import cuda_graph_indices
    class Indexed(torch.nn.Module):
        def forward(self, x):
            return x[torch.arange(x.shape[0], device='cpu'), 0]
    x = torch.randn(1, 3, 8, 8, device='cuda')
    model = torch.jit.freeze(torch.jit.trace(Indexed().eval(), x))
    expected = model(x).clone()
    # Use a fresh unexecuted frozen module, just like adapter startup.
    model = torch.jit.freeze(torch.jit.trace(Indexed().eval(), x))
    cuda_graph_indices(model, torch)
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        for _ in range(5):
            model(x)
    stream.synchronize()
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph, stream=stream):
        actual = model(x)
    graph.replay()
    torch.cuda.synchronize()
    assert torch.equal(actual, expected)
