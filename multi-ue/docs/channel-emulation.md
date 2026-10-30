# Aetheredge channel-emulation runbook

The campaign uses one symbol-static SISO realization for the aggregate radio
waveform in both UL and DL. Statistical-model `delay_spread_ns` is RMS delay
spread. The gNB CIR controller listens on TCP port 5560 at its static
`oai-e2-net` address, `192.168.73.140`, and starts in noise-free pass-through.
A scenario selects a complete precomputed file trace; no per-slot ZMQ messages
are sent.

## Build the trace bank

Start the rebuilt gNB once, then capture its actual radio parameters:

```bash
cd ~/mec-offload-ad
mkdir -p multi-ue/runs/campaigns/aetheredge-fixed-policy-v1
mkdir -p gnb-core/plugins/channel_emulation/data/trace_bank

gnb-core/env/bin/python multi-ue/tools/channel_control.py config \
  --endpoint tcp://192.168.73.140:5560 \
  > multi-ue/runs/campaigns/aetheredge-fixed-policy-v1/channel-loader-config.json
```

Stop the gNB while Sionna generates the bank, then run:

```bash
cd ~/mec-offload-ad
MPLCONFIGDIR=/tmp/aetheredge-matplotlib \
gnb-core/env/bin/python multi-ue/tools/build_channel_trace_bank.py \
  --campaign multi-ue/config/campaigns/aetheredge-fixed-policy-v1.yaml \
  --loader-config multi-ue/runs/campaigns/aetheredge-fixed-policy-v1/channel-loader-config.json \
  --output gnb-core/plugins/channel_emulation/data/trace_bank
```

The generator writes one directory for each of 111 conditions and records
Sionna, TensorFlow, radio, seed, retained-energy, and SHA-256 metadata. A
repeated command validates and reuses completed directories, so an interrupted
generation resumes without regenerating valid traces. Do not pass
`--overwrite` when resuming.

## Preflight and selection

After restarting the gNB, validate the entire bank and activate a pilot trace:

```bash
cd ~/mec-offload-ad
gnb-core/env/bin/python multi-ue/tools/channel_preflight.py \
  --campaign multi-ue/config/campaigns/aetheredge-fixed-policy-v1.yaml \
  --trace-bank gnb-core/plugins/channel_emulation/data/trace_bank \
  --gnb-config gnb-core/config/common/gnb.sa.band78.106prbs.conf \
  --compose gnb-core/config/common/docker-compose.yaml \
  --endpoint tcp://192.168.73.140:5560 \
  --select-condition awgn-sinr20
```

The selected generation becomes effective on the next emulator read. The
loader reuses a trace slot for the RX and TX calls before advancing. A scenario
runner must record the returned condition ID, trace hash, generation, and
status with the attempt evidence before it starts UE media.

Individual control operations are also available:

```bash
gnb-core/env/bin/python multi-ue/tools/channel_control.py status

gnb-core/env/bin/python multi-ue/tools/channel_control.py select \
  --trace-bank gnb-core/plugins/channel_emulation/data/trace_bank \
  --condition-id tdl-c-sinr10-delay-spread100-speed1
```

## Required live calibration

Before campaign collection, run pass-through and AWGN 0/10/20 dB pilots.
Compare requested SINR with OAI PUSCH/PUCCH SNR, BLER, MCS, retransmissions,
and throughput. The SRK emulator normalizes CIRs and clips noise, so requested
SINR must be reported separately from measured radio conditions.

## 51-PRB three-quarter-sampling campaign

The 51-PRB campaign uses a separate immutable trace bank and scenario space:

```text
campaign:   config/campaigns/aetheredge-fixed-policy-51prb-v1.yaml
manifest:   runs/campaigns/aetheredge-fixed-policy-51prb-v1/
trace bank: ../gnb-core/plugins/channel_emulation/data/trace_bank-51prb
radio:      51 PRBs, FFT 768, 23.04 MHz, 3.30948 GHz, -E enabled
```

Set the following in `gnb-core/config/b200/.env` when activating this profile.
The container destination stays constant so the loader command does not need a
different trace-root path:

```bash
CHANNEL_TRACE_BANK=../../plugins/channel_emulation/data/trace_bank-51prb
```

After the 51-PRB gNB starts, online preflight must confirm that the loader and
bank agree before running a scenario:

```bash
gnb-core/env/bin/python multi-ue/tools/channel_preflight.py \
  --campaign multi-ue/config/campaigns/aetheredge-fixed-policy-51prb-v1.yaml \
  --trace-bank gnb-core/plugins/channel_emulation/data/trace_bank-51prb \
  --gnb-config gnb-core/config/common/gnb.sa.band78.51prbs.conf \
  --compose gnb-core/config/common/docker-compose.yaml \
  --endpoint tcp://192.168.73.140:5560 \
  --select-condition awgn-sinr20
```
