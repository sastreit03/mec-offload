"""Capture selected production artifacts in isolation; print full failure chains."""
import argparse
import json
import traceback
from pathlib import Path
from mec_server.config import load
from mec_server.workers import TorchAdapter

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application',default='detection')
    parser.add_argument('--model',default='yolo26x')
    parser.add_argument('--weights',default='/weights')
    args=parser.parse_args()
    _,catalog,_=load(Path('config/mec.collection.yaml'))
    specs={c['model']:c for c in catalog['configurations']
           if c['application']==args.application and c['model']==args.model}
    assert specs
    try:
        adapter=TorchAdapter(specs,args.weights,[-3],False,cuda_graphs=True)
        adapter.prepare_graphs(specs)
        print(json.dumps(dict(status='passed',models=list(specs),graphs=len(adapter.graphs))))
        adapter.close()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
