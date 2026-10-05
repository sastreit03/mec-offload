# Validation performed

Date: 5 October 2026. Python 3.12, CPU environment.

- `python -m unittest discover -s tests -v`: 22 tests discovered;
  21 passed, one H.264/GStreamer integration test explicitly skipped because
  PyGObject/GStreamer are unavailable here.
- Separate server and UE CLI processes: two 20-frame video tasks completed;
  all 40 frame jobs and UE result-receipt records were present. The UE resumed
  the same identity, the second task reused the same worker, and SIGTERM wrote
  the final stopped snapshot.
- Python compilation, shell syntax, default configuration and both catalogs
  were checked successfully.

CUDA, MPS attachment, actual stream priority behavior, the YOLO checkpoint path,
and the GStreamer H.264 end-to-end path were not executed in this environment.
Run the included H.264 integration test and `scripts.check_gpu_pool` on the Spark.
The unavailable hardware paths are implemented but remain unverified there;
the CPU checks do not establish GPU overlap, latency/accuracy SLO guarantees,
or deployment memory requirements.
