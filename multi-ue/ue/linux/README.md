Run this client inside the softUE network namespace after OTA attachment. It does not replace the OAI softUE or configure its B210.

Install distro GStreamer Python bindings, plugins-good/bad/ugly/libav, OpenCV and the Python requirements. Source `multi-ue/scripts/environment.sh`, then:

```bash
python -m mec_ue.client --server 192.168.72.136 --source-ip 12.1.1.2 --source /datasets/clip.mp4 --application detection --logs runs/ue-a
```

For a camera use `--source 0`. `--source-ip` must be the softUE interface address, not the host management address. `scripts/softue-client.sh` obtains the current IPv4 address automatically from an existing softUE container; use that wrapper for normal runs. Android obtains its cellular address itself. No RNTI is supplied by either client.
