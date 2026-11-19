# Pilot campaign results

Only accepted valid attempts; frames selected by MEC first packet arrival in [1 s, 6 s) after scenario release. Results arriving later remain included. SLO: completed result received within 150 ms, divided by selected transmitted frames. P99 uses linear interpolation.

## Single UE

| UE / application | Transmitted | Receipts | Mean ms | P99 ms | SLO success |
|---|---:|---:|---:|---:|---:|
| All UEs | 602 | 602 | 304.141 | 4694.408 | 35.55% |
| pixel1 | 602 | 602 | 304.141 | 4694.408 | 35.55% |
| All / detection | 200 | 200 | 190.777 | 432.015 | 51.50% |
| All / segmentation | 198 | 198 | 203.856 | 313.777 | 16.67% |
| All / tracking | 204 | 204 | 512.617 | 4946.302 | 38.24% |
| pixel1 / detection | 200 | 200 | 190.777 | 432.015 | 51.50% |
| pixel1 / segmentation | 198 | 198 | 203.856 | 313.777 | 16.67% |
| pixel1 / tracking | 204 | 204 | 512.617 | 4946.302 | 38.24% |

### MEC breakdown (all UEs)

| Stage | Samples | Mean ms | P99 ms |
|---|---:|---:|---:|
| mec_decode | 582 | 6.237 | 10.064 |
| mec_queue_wait | 582 | 14.585 | 317.870 |
| mec_preprocess | 582 | 0.597 | 0.969 |
| mec_gpu_inference | 582 | 17.570 | 48.166 |
| mec_postprocess | 582 | 1.211 | 26.169 |
| mec_total_model_processing | 582 | 19.397 | 51.606 |
| mec_reorder_wait | 185 | 191.306 | 4166.879 |
| mec_tracking | 185 | 1.730 | 4.780 |
| mec_result_serialization | 602 | 1.240 | 5.127 |
| mec_cuda_event | 582 | 17.561 | 48.156 |
| mec_frame_reception_span | 602 | 24.723 | 86.481 |
| mec_reception_to_result_ready | 602 | 177.698 | 4486.670 |
| mec_first_packet_to_result_ready | 602 | 202.421 | 4500.710 |

## Four UEs

| UE / application | Transmitted | Receipts | Mean ms | P99 ms | SLO success |
|---|---:|---:|---:|---:|---:|
| All UEs | 2444 | 2444 | 497.214 | 6488.827 | 27.70% |
| linux1 | 632 | 632 | 367.947 | 2257.664 | 58.70% |
| pixel1 | 603 | 603 | 470.500 | 2370.106 | 17.08% |
| pixel2 | 604 | 604 | 797.865 | 8127.332 | 17.05% |
| pixel3 | 605 | 605 | 358.722 | 1140.661 | 16.53% |
| All / detection | 817 | 817 | 366.429 | 1688.150 | 32.19% |
| All / segmentation | 809 | 809 | 513.208 | 2359.396 | 12.61% |
| All / tracking | 818 | 818 | 612.022 | 7925.324 | 38.14% |
| linux1 / detection | 218 | 218 | 255.153 | 907.536 | 61.01% |
| linux1 / segmentation | 212 | 212 | 741.329 | 2331.571 | 18.87% |
| linux1 / tracking | 202 | 202 | 97.809 | 151.465 | 98.02% |
| pixel1 / detection | 200 | 200 | 400.554 | 1326.893 | 28.50% |
| pixel1 / segmentation | 198 | 198 | 751.910 | 2515.086 | 2.02% |
| pixel1 / tracking | 205 | 205 | 266.940 | 692.015 | 20.49% |
| pixel2 / detection | 201 | 201 | 368.256 | 1914.218 | 21.39% |
| pixel2 / segmentation | 200 | 200 | 231.234 | 444.002 | 15.50% |
| pixel2 / tracking | 203 | 203 | 1781.499 | 8486.406 | 14.29% |
| pixel3 / detection | 198 | 198 | 452.623 | 1201.456 | 15.15% |
| pixel3 / segmentation | 199 | 199 | 316.070 | 795.515 | 13.57% |
| pixel3 / tracking | 208 | 208 | 310.142 | 856.236 | 20.67% |

### MEC breakdown (all UEs)

| Stage | Samples | Mean ms | P99 ms |
|---|---:|---:|---:|
| mec_decode | 2418 | 6.700 | 15.443 |
| mec_queue_wait | 2418 | 79.444 | 1959.990 |
| mec_preprocess | 2418 | 0.678 | 1.331 |
| mec_gpu_inference | 2418 | 19.395 | 57.977 |
| mec_postprocess | 2418 | 1.354 | 26.665 |
| mec_total_model_processing | 2418 | 21.456 | 62.985 |
| mec_reorder_wait | 810 | 381.389 | 7678.546 |
| mec_tracking | 810 | 3.858 | 14.648 |
| mec_result_serialization | 2444 | 1.917 | 8.533 |
| mec_cuda_event | 2418 | 19.383 | 57.966 |
| mec_frame_reception_span | 2441 | 47.716 | 244.983 |
| mec_reception_to_result_ready | 2441 | 243.882 | 6198.774 |
| mec_first_packet_to_result_ready | 2444 | 291.718 | 6289.423 |

## Evidence and limitations

Window accounting: `{'outside_window': 1130, 'selected': 3046, 'unknown_window': 1}`. Issues: 0. See summary.json for source paths, accepted attempts, duplicates, and methodology.

MEC breakdowns per UE and application are in the JSON summaries. Linux logs compute latency and receipt timestamps with separate clock reads; this report consistently recomputes latency from the logged receipt and transmission timestamps. Missing stage samples are not zero latency. CUDA event and inference timings overlap; do not add them. These aggregate historical pilot deployments, not a single uniform software version. EdgeRIC finalization confirmations are checked from runner evidence; raw EdgeRIC logs are not needed for these latency metrics and have not been exhaustively audited.

Reproduce from repository root:

```bash
python3 runs/campaigns/20261117-pilot-mps/results/analyze.py
```
