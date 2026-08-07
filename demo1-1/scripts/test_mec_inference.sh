#!/bin/bash
#
# test_mec_inference.sh
#
# Purpose: Script to be run on UE.
#          Run an inference test on MEC server and receive results.
#
# Prerequisites:
# - Must be run on UE PC.
# - Script must be run in directory mec-offload-ad/demo1-1/scripts.
# - OAI 5GC, gNB, and UE docker containers must be running.
# - mec-yolo container must be running.
# - If necessary, change source_ip if UE disconnects and reconnects with
#   new IP address
#
# Acknowledgement: Commands below were written by Generative AI.

set -e  # Stop script on any error
set -o pipefail

UE_CONTAINER=oai-nr-ue
SOURCE_IP=12.1.1.2
MEC_IP=192.168.72.136
IMAGE_PATH=/tmp/coco_test.jpg

# Copy test image to docker container
docker cp ../ue-client/coco_test.jpg "$UE_CONTAINER":"$IMAGE_PATH"

# Run test
docker exec -i "$UE_CONTAINER" python3 - <<PY | tee ../ue-client/mec-inference-test.txt
import http.client
import json
import uuid

source_ip = "$SOURCE_IP"
mec_ip = "$MEC_IP"
image_path = "$IMAGE_PATH"

boundary = "----MECBoundary"
task_id = f"ue-test-{uuid.uuid4()}"

with open(image_path, "rb") as f:
    image_data = f.read()

parts = []

def add_field(name, value):
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n".encode()
    )

add_field("task_id", task_id)
add_field("conf", "0.25")
add_field("iou", "0.70")
add_field("imgsz", "640")

parts.append(
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="image"; filename="coco_test.jpg"\r\n'
    f"Content-Type: image/jpeg\r\n\r\n".encode()
    + image_data
    + b"\r\n"
)

parts.append(f"--{boundary}--\r\n".encode())
body = b"".join(parts)

headers = {
    "Content-Type": f"multipart/form-data; boundary={boundary}",
    "Content-Length": str(len(body)),
}

conn = http.client.HTTPConnection(
    mec_ip,
    8080,
    timeout=30,
    source_address=(source_ip, 0),
)

conn.request("POST", "/v1/detect", body=body, headers=headers)
response = conn.getresponse()
response_body = response.read().decode("utf-8", errors="replace")

print("HTTP status:", response.status)

try:
    print(json.dumps(json.loads(response_body), indent=2))
except json.JSONDecodeError:
    print(response_body)
PY
