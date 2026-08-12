# PCAP Commands

## Purpose

Contains commands to be run for packet captures. Not intended to be run as one
script.

## Prerequisites

- OAI gNB, 5GC, and UE Docker containers must be running.
- The original UE container must be modified to have the `requests` and
  `opencv-python-headless` Python packages.
- The image `coco_test.jpg` must be in the directory `../ue-client/`.

> **Acknowledgement:** Commands below were written by Generative AI.

## Code Sequence

### 1. Open terminals

Open two extra terminals on the gNB PC, one for the N3 capture and one for the N6
capture. Open one extra terminal on the UE PC for the UE tunnel interface
capture.

### 2. Set run and task ID names on all three terminals

On all three terminals:

```bash
RUN_ID=$(date +%Y%m%d-%H%M%S)
echo "RUN_ID=$RUN_ID"
```

### 3. Set parameters on the UE interface terminal

On the UE PC:

```bash
UE_CONTAINER=oai-nr-ue
UE_TUN=$(docker exec "$UE_CONTAINER" ip -o link show | awk -F': ' '$2 ~ /^oaitun_ue/ {print $2; exit}')
UE_IP=$(docker exec "$UE_CONTAINER" ip -4 -o addr show dev "$UE_TUN" | awk '{print $4}' | cut -d/ -f1 | head -n1)
MEC_IP=192.168.72.136

printf 'UE_TUN=%s\nUE_IP=%s\n' "$UE_TUN" "$UE_IP"
```

### 4. Set parameters on the N3 and N6 terminals

On both the N3 and N6 terminals:

```bash
EXT_DN_CONTAINER=oai-ext-dn
UE_IP=<UE_PDU_SESSION_IP>
MEC_IP=192.168.72.136
```

### 5. Start the capture on the UE interface terminal

On the UE PC:

```bash
UE_PID=$(docker inspect -f '{{.State.Pid}}' "$UE_CONTAINER")

sudo nsenter -t "$UE_PID" -n -- \
  tcpdump -U -ni "$UE_TUN" \
  "host $MEC_IP and tcp port 8080" \
  -w "/tmp/demo1-${RUN_ID}-ue-tun.pcap"
```

### 6. Start the capture on the N3 terminal

On the N3 terminal:

```bash
sudo tcpdump -U -ni any \
  "udp port 2152" \
  -w "demo1-${RUN_ID}-n3.pcap"
```

### 7. Start the capture on the N6 terminal

On the N6 terminal:

```bash
EXT_DN_PID=$(docker inspect -f '{{.State.Pid}}' "$EXT_DN_CONTAINER")
sudo nsenter -t "$EXT_DN_PID" -n -- tcpdump -ni any tcp port 8080 -w "demo1-${RUN_ID}-n6.pcap"
```

### 8. Send the inference request

Open a separate terminal on the UE PC and run the following script:

```bash
~/mec-offload-ad/demo1/scripts/run_ue_client.sh
```

### 9. Stop the captures

On all three capture terminals, press <kbd>Ctrl</kbd>+<kbd>C</kbd>.

### 10. Copy the UE PCAP file from the Docker container

On the UE interface terminal:

```bash
sudo mv \
  "/tmp/demo1-${RUN_ID}-ue-tun.pcap" \
  "./demo1-${RUN_ID}-ue-tun.pcap"
```

### 11. View the PCAP files in Wireshark

Expected results:

- **UE interface:** Only contains packets between the UE IP address and the MEC
  IP address.
- **N3 interface:** Captures packets between the UE IP address and the MEC IP
  address.
- **N6 interface:** Due to SNAT on the UPF, the packets will be between the UPF
  N6 IP address (`192.168.72.134`) and the MEC IP address.
