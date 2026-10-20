"""Use exact copies of the attached server modules, without changing their contents.
Run: python tests/test_server_contract.py --server-dir /path/to/canonical/mec_app
In this development workspace, --server-dir upload selects the attachment filenames.
"""
import argparse
import asyncio
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from PIL import Image


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--server-dir',required=True);parser.add_argument('--mjpeg-fixture')
    args=parser.parse_args();directory=Path(args.server_dir)
    with tempfile.TemporaryDirectory() as temporary:
        for plain,attached in [('models.py','models(4).py'),('control.py','control(5).py'),('dashboard.py','dashboard(4).py'),('scheduler.py','scheduler(2).py'),('vlm.py','vlm(3).py')]:
            source=directory/plain if (directory/plain).exists() else directory/attached
            shutil.copyfile(source,Path(temporary)/plain)
        sys.path.insert(0,temporary)
        from models import SharedState, FrameBuffer, AppConfig, DecodedFrame
        from control import ControlHub
        from dashboard import router,preview
        from scheduler import InferenceScheduler
        state=SharedState();frames=FrameBuffer(180);cfg=AppConfig()
        app=FastAPI();app.include_router(router)
        app.state.shared=state;app.state.frame_buffer=frames;app.state.config=cfg
        app.state.ran_telemetry=SimpleNamespace(snapshot=lambda:{'connected':True,'stale':False,'telemetry':{'mac':[]}})
        hub=ControlHub(state,frames,cfg.vlm)
        @app.websocket('/ws/ue')
        async def control(ws:WebSocket):await hub.websocket_session(ws)
        state.set_receiver_running(True)
        image=Image.fromarray(np.zeros((32,64,3),dtype=np.uint8));out=io.BytesIO();image.save(out,'JPEG');state.set_preview(out.getvalue(),None)
        with TestClient(app) as client:
            status=client.get('/api/status').json()
            assert set(status)=={'state','ran_telemetry','config'}
            assert status['state']['receiver']['preview_sequence']==1
            assert 'rtp' in status['state'] and 'latest_result' in status['state']['control']
            with client.websocket_connect('/ws/ue') as ws:
                assert ws.receive_json()['type']=='mec_hello'
                video={'codec':'H264','rtp_payload_type':96,'fps':5,'width':640,'height':360,'bitrate_kbps':400,'gop_frames':10,'b_frames':0,'rtp_mtu':1200,'video_port':5000}
                ws.send_json({'type':'stream_config','video':video});assert ws.receive_json()['video']==video
                ws.send_json({'type':'question','question_id':100,'text':'What changed?'});ack=ws.receive_json();assert ack['type']=='question_ack' and ack['question_id']==100
                ws.send_json({'type':'question','question_id':'bad','text':'x'});assert ws.receive_json()['error']=='invalid_question'
        class Request:
            app=SimpleNamespace(state=SimpleNamespace(shared=state))
            async def is_disconnected(self):return False
        async def multipart():
            response=await preview(Request());chunk=await response.body_iterator.__anext__();await response.body_iterator.aclose();return chunk
        chunk=asyncio.run(multipart());assert b'Content-Length:' in chunk and out.getvalue() in chunk
        if args.mjpeg_fixture:Path(args.mjpeg_fixture).write_bytes(chunk)
        class Engine:
            ready=True
            def __init__(self):self.calls=0
            def infer(self,**kwargs):self.calls+=1;return {'answer':'Compatible flat result','confidence':.5,'event_detected':None,'details':'Test fixture'}
        class Control:
            def __init__(self):self.messages=[]
            async def send(self,message):self.messages.append(message)
        async def scheduler_check():
            engine=Engine();control=Control();scheduler=InferenceScheduler(cfg.vlm,state,frames,engine);scheduler.attach_control(control)
            for n in range(3):frames.append(DecodedFrame(n,time.time_ns(),time.monotonic_ns(),None,64,32,np.zeros((32,64,3),dtype=np.uint8)))
            await scheduler._maybe_infer();await scheduler._maybe_infer()
            assert engine.calls==1,'Unchanged server must infer once per question ID'
            m=control.messages[0];assert m['answer']=='Compatible flat result' and m['frame_count']==3 and 'result' not in m
            state.set_question({'type':'question','question_id':101,'text':'What changed?'})
            for n in range(3,6):frames.append(DecodedFrame(n,time.time_ns(),time.monotonic_ns(),None,64,32,np.zeros((32,64,3),dtype=np.uint8)))
            await scheduler._maybe_infer();assert engine.calls==2 and control.messages[-1]['question_id']==101
        asyncio.run(scheduler_check())
        print('Unchanged server contracts passed: status envelope, WS messages, JPEG multipart, flat results, one answer per ID, new-ID re-ask')
if __name__=='__main__':main()
