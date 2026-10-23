#!/usr/bin/env python3
import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path

# Collect environment evidence; discovery alone does not establish live readiness.
def main():
 p=argparse.ArgumentParser();p.add_argument('--gpu',action='store_true');p.add_argument('--output',default='runs/preflight.json');a=p.parse_args();out=dict(python=platform.python_version(),platform=platform.platform(),docker=bool(shutil.which('docker')))
 for cmd in [['gst-inspect-1.0','--version'],['nvidia-smi']]:
  try:r=subprocess.run(cmd,capture_output=True,text=True,timeout=15);out[cmd[0]]=dict(code=r.returncode,stdout=r.stdout,stderr=r.stderr)
  except Exception as e:out[cmd[0]]=dict(error=str(e))
 if a.gpu:
  import torch
  out['torch']=torch.__version__;out['cuda']=torch.version.cuda;out['available']=torch.cuda.is_available()
  if torch.cuda.is_available():out['device']=torch.cuda.get_device_name();out['streams']={str(p):torch.cuda.Stream(priority=p).priority for p in [-5,-3,0]}
  out['mps_pipe']=Path('/tmp/nvidia-mps/control').exists()
 path=Path(a.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
if __name__=='__main__':main()
