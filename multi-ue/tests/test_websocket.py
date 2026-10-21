from pathlib import Path
import time
from fastapi.testclient import TestClient
from mec_server.config import load
from mec_server.engine import Engine
from mec_server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_control_socket_identity_admission_retry_and_resume(tmp_path):
    cfg, c, p = load(ROOT / "config/mec.development.yaml")
    cfg.runs_dir = str(tmp_path)
    cfg.control_port = 23470
    e = Engine(cfg, c, p)
    with TestClient(create_app(e)) as client:
        for _ in range(200):
            if client.get("/readyz").status_code == 200:
                break
            time.sleep(0.01)
        assert client.get("/healthz").json()["status"] == "alive"
        with client.websocket_connect("/ws/ue") as socket:
            hello = socket.receive_json()
            assert hello["type"] == "mec_hello"
            uid = hello["ue_id"]
            socket.send_json(dict(type="time_sync", client_mono_ns=1))
            sync = socket.receive_json()
            assert sync["server_send_unix_ns"] >= sync["server_receive_unix_ns"]
            start = dict(
                type="video_start",
                ue_id=uid,
                client_seq=1,
                application="detection",
                input=dict(width=1280, height=720, fps=30),
            )
            socket.send_json(start)
            while True:
                response = socket.receive_json()
                if response["type"] == "video_response":
                    break
            assert response["accepted"]
            socket.send_json(start)
            assert socket.receive_json()["video_id"] == response["video_id"]
        with client.websocket_connect(
            "/ws/ue", headers={"x-mec-resume-token": hello["resume_token"]}
        ) as socket:
            resumed = socket.receive_json()
            assert (
                resumed["ue_id"] == uid
                and resumed["active_video_id"] == response["video_id"]
                and resumed["connection_id"] != hello["connection_id"]
            )
