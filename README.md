# mec-offloading-ad
Contains code for mobile edge computing (MEC) 5G O-RAN task offloading of anomaly detection tasks.

This private repository tracks three modified copies of NVIDIA's public sionna-rk repository.

## Layout

- `core-mec/`: code and configuration for the DGX Spark functioning as the core network and MEC server.
- `gnb/`: code and configuration for the DGX Spark functioning as the gNB.
- `ue/`: code and configuration for the PC functioning as the UE.

The three folders intentionally contain separate copies so that core/MEC-, gNB-, and UE-side changes can be tracked independently.
