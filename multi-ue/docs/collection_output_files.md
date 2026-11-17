# Collection output files

This document describes the files written by the Linux and Pixel UEs, MEC,
EdgeRIC, the campaign runner, and the dataset exporter. It describes the current
implementation in [export_campaign_dataset.py](../tools/export_campaign_dataset.py).

Raw files contain measurements and operational diagnostics, including failed
attempts and data outside the collection window. The exporter creates a separate
dataset; it does not trim or modify the raw source files.

## Formats and identifiers

- **JSONL:** each line is a JSON object. The `type` field identifies the event or
  message. Different event types can have different fields in the same file.
- **JSON:** one document, often a snapshot or report; there are no independent
  text rows. Arrays and mappings inside the document contain individual entries.
- **CSV:** the first line is a header; each subsequent row follows that schema.
  Fields ending in `_json` hold serialized JSON, quoted as needed by CSV.
- **SQLite:** a database containing tables and views. The tables below describe
  what each logical row represents, rather than a physical line in the file.

Campaign records are associated using `campaign_id`, `scenario_id`, and
`attempt_id`. Frame records additionally use `video_id` and `frame_id`. `ue_id`
identifies the registered UE; `ue_alias` is its campaign name, such as `pixel1`
or `linux1`. Do not join records on `frame_id` alone.

## Raw UE files

Linux writes to the directory specified by `--logs` (default: `ue-run`, relative
to the client working directory). Pixel writes to its app-specific external-files
directory, normally `/sdcard/Android/data/edu.clemson.mecrtp/files/`.

| File | Contents and meaning of a row | Source of its contents | Used by the exporter? |
| --- | --- | --- | --- |
| Linux `events.jsonl` | One client event per line: a fully submitted frame (`transmitted`), result receipt (`result_receipt`), media preparation/end, scenario start/abort, association probe, reconnect, or error. | Linux client and media sender callbacks, plus messages received from MEC. | Only `transmitted` and `result_receipt`, when supplied through `--ue-log linux1=PATH`. |
| Pixel `events.jsonl` | One Android client event per line. Includes transmissions, result receipts, media and scenario lifecycle, probes, and client/network errors. | Android app and media sender callbacks, plus messages received from MEC. | Only `transmitted` and `result_receipt`, when supplied through `--ue-log ALIAS=PATH`. |
| UE `receipts.sqlite3` | Table `seen`: one row containing a received result ID. Prevents handling replayed results twice. Both UE clients have this file. | Result messages received from MEC. | No. Receipt measurements come from `events.jsonl`, not this database. |
| Linux `identity.json` | One saved state object containing the command sequence counter and MEC resume token. | Client control state and the token received from MEC. | No. This is reconnection state. |

`transmitted` means the UE finished submitting the frame's packets; it does not
prove that MEC received them. `result_receipt` records the result reaching the UE,
with latency and SLO success when the required transmission timing is available.
UE monotonic timestamps must not be directly subtracted from MEC monotonic
timestamps: they belong to different device clocks.

Both clients can record `deadline_miss` before a late result arrives. The exporter
does not import that event. It instead imports the eventual receipt's latency and
SLO result. Missing receipt fields do not mean success. `final_missing` is disabled
in collection mode so that draining MEC work can finish and deliver late results.

These event logs append to existing files. Reusing a log directory or Pixel app
storage can retain multiple runs and attempts in the same file.

## Raw MEC files

With the current container mounts and configuration, these files are on MEC Spark
under `~/mec-offload-ad/multi-ue/runs/<run_id>/mec/`. The actual directory follows
`runs_dir` and `run_id` in the selected MEC configuration.

| File | Contents and meaning of a row | Source of its contents | Used by the exporter? |
| --- | --- | --- | --- |
| `jobs.jsonl` | One terminal frame-result record per line. Includes frame/session identity, outcome (`completed`, `failed`, or `dropped`), reason, processing configuration, and `stage_times`. The inference `payload` is omitted. Missing-frame failures can have empty `stage_times`. | MEC receiver, scheduler, processing workers, and result-delivery preparation. | Yes: primary source of frame rows and their outcomes/timings. |
| `events.jsonl` | One measurement or operational event per line. Includes descriptors, resource snapshots, configuration decisions, connections, scenario lifecycle, and receiver errors. | MEC control, receiver, scheduler, telemetry, and lifecycle code. | Yes, but only the event types listed below. |
| `run.json` | Latest run snapshot: settings, UE connections, video states/counters, and outstanding work. Replaced when updated. | Current in-memory MEC state. | No. The exporter obtains accepted-attempt evidence from the runner checkpoint. |
| `replay.sqlite3` | Table `results`: one stored result identified by result ID, with UE, video, frame, and serialized full message in `payload`. Unlike `jobs.jsonl`, the message retains its inference payload. | MEC result delivery. Read by the control channel to replay results after reconnection. | No. |

The exporter recognizes these MEC event types:

| Event type | Export destination and purpose |
| --- | --- |
| `descriptor` | `descriptors`; supplies received frame metadata, encoded size/type, and available transmission/arrival timing for joins. |
| `mec_resources` | `mec_resources`; supplies worker, stream, and CPU and GPU resource measurements. |
| `packet_loss` | `media_events`; receiver diagnostic for a missing RTP packet. |
| `media_error` | `media_events`; receiver or decoding diagnostic. |
| `pipeline` | `media_events`; receiver setup, GStreamer, port, and buffering configuration. Usually occurs before the selected window and is removed. |
| `input_closed` | `media_events`; when and why MEC closes video input. Normal closure is after the selected window and is removed. |
| `receiver_setup_failed` | `media_events`; receiver initialization failure. Normally prevents a valid attempt, so it will not appear in accepted-attempt data. |
| `collection_run_invalid` | Recognized by the importer, but invalid attempts are excluded from the final dataset. |

Other MEC event types are not directly imported. For example, the separate
`residence` event records a sample used by MEC's internal residence-time estimator;
the exporter uses job stage timestamps instead and does not preserve the exact
`residence_s` sample. The start time comes from runner evidence, not from importing
the `scenario_start_released` log event.

## Raw EdgeRIC files

On RAN Spark, the current configuration writes under
`~/mec-offload-ad/multi-ue/runs/edgeric/<source-generation>/`. A new EdgeRIC process
uses a new source-generation identifier and directory.

| File | Contents and meaning of a row | Source of its contents | Used by the exporter? |
| --- | --- | --- | --- |
| `events.jsonl` | One event per line. A `radio_report` contains an aggregated measurement interval and a mapping of UE measurements. Other types describe association loss/finalization, probes, synchronization, resource snapshots received from MEC, and diagnostics. | RAN telemetry from gNB, EdgeRIC association/aggregation logic, and messages from MEC. | Only `radio_report`. Each report can produce several dataset rows, one per UE. |
| `associations.json` | Snapshot of source identity, UE-to-RNTI bindings, lease/generation information, and association-match results. Periodically replaced and written during shutdown. | Current EdgeRIC association state. | No. Exported association information comes from radio reports. |

The exporter does not import EdgeRIC's copy of `mec_resources`; it reads resource
snapshots from MEC's own `events.jsonl`.

### CPU and GPU resource sampling

MEC emits `mec_resources` on the configured 100-ms reporting schedule. Its `gpu`
object contains device-wide NVML measurements. Its `cpu` object uses psutil's
nonblocking CPU utilization calls and contains:

- `valid`: whether a measured utilization interval is available.
- `scope`: `host; not MEC process only`. Other host workloads are included.
- `measurement_monotonic_ns`: MEC timestamp of the sampling call.
- `interval_s`: actual elapsed time since the preceding successful sample.
- `utilization_percent`: aggregate host CPU utilization (0–100%).
- `per_cpu_utilization_percent`: one utilization percentage per logical CPU.

The first CPU sample establishes a baseline and is marked invalid, with
`reason: cpu_baseline_pending` and no utilization values. Sampling failures are
also marked invalid; after an error, a new baseline is established. Invalid
samples are not reported as zero load. Sampling does not sleep to measure an
interval or introduce additional control messages.

The exporter preserves the CPU object in `cpu_json` in the `mec_resources`
table/CSV and `frame_mec_features` view/CSV, alongside `gpu_json`. Older logs
without a CPU object export JSON `null` in that column. The original resource
event, including its CPU object, is also preserved in `payload_json`.

## Campaign files

These live in the campaign directory passed to the runner and to the exporter's
`--campaign-dir`. They are distinct from the per-component log directories.

| File | Contents and meaning of a row | Source of its contents | Used by the exporter? |
| --- | --- | --- | --- |
| `scenarios.jsonl` | One scenario specification per line: identifiers, execution order, UE assignments, applications/candidates, media selections, radio/channel settings, and experiment factors. | Campaign generation. This is the requested configuration, not measured behavior. | Yes: scenario metadata and UE assignments. |
| `manifest.lock.json` | One manifest-integrity document, including the scenarios hash and count. | Campaign generation. The runner checks it before running. | Its file metadata/hash is included in export provenance when present. It does not supply frame measurements. |
| `state.sqlite3` | Table `scenarios`: one mutable checkpoint row per scenario, containing status, attempt count, current/latest attempt ID, timestamps, error, and accepted runner evidence. Table `metadata` stores checkpoint identity information. This is not one historical row per attempt. | Runner; accepted evidence includes MEC start response, final confirmation, channel evidence, and video status/counters. | Yes: selects valid attempts and supplies start timestamps and full-attempt evidence. |
| `runner-events.jsonl` | One runner event per line: preflight waits, attempt/recovery activity, start release, success/failure, campaign abort, and completion. | Runner execution and component/API responses. | No. Useful for reconstructing campaign history. |
| `runner-status.json` | Latest runner event and checkpoint status counts. Replaced on updates. | Runner event writer and checkpoint counts. | No. |

`runner.lock` is an operational locking file, not a results table. Recovery may
also preserve gNB logs and inspection output under the campaign's `recovery/`
directory. Those files are diagnostics, not exporter inputs.

## Final dataset selection

The exporter writes to `--output`. Selection follows these rules:

1. Use the attempt recorded as `valid` in each scenario's checkpoint. Rejected
   attempts remain in raw logs; they do not supply measurement rows.
2. Validate full-attempt terminal-frame totals before filtering.
3. Read the accepted attempt's `release.start_monotonic_ns` from runner evidence.
4. Keep frames whose MEC `first_packet_monotonic_ns` is at least one second and
   less than six seconds after that start: **[1 s, 6 s)**.
5. Exclude frames without usable arrival timestamps and count them as
   `unknown_window`. They cannot be reliably placed in the five-second interval.
6. Keep selected frames' outcomes and later processing/receipt measurements even
   when those measurements occur after six seconds. Failed frames with eligible
   arrival timestamps remain included.
7. Remove unrelated descriptors, UE evidence, and measurement-interval links.
   Retain measurement intervals needed by selected frames without changing their
   original bounds. Filter `media_events` by each event's own MEC monotonic time.

Missing-frame exclusions can make loss rates calculated only from exported frame
rows understate total loss. Full-attempt totals remain available in metadata.
An accepted attempt without a usable MEC start timestamp cannot be exported.

## Final output files and their sources

`dataset.sqlite3` is the canonical combined database. The seven CSVs are exports
of its tables or views, not independently calculated results. All are written by
[export_campaign_dataset.py](../tools/export_campaign_dataset.py).

| Output file | Contents and meaning of a row | Input files and source components |
| --- | --- | --- |
| `dataset.sqlite3` | Combined tables and views listed in the next section. Contains selected frame measurements, supporting evidence, and scenario/video metadata. | Campaign `scenarios.jsonl` and `state.sqlite3`; MEC `jobs.jsonl` and `events.jsonl`; EdgeRIC `events.jsonl`; optional UE `events.jsonl`. |
| `scenarios.csv` | One row per scenario in the manifest, including configuration, checkpoint status, attempt information, and serialized manifest/runner evidence. Can include pending or failed scenario metadata; this does not admit their measurements. | Campaign `scenarios.jsonl` plus `state.sqlite3`. |
| `videos.csv` | One video/session row from accepted-attempt runner evidence. Includes UE alias, application/candidate, source assignment, terminal state, full-attempt outcome counts, sent/received counts, and count-validation flag. | Campaign `state.sqlite3` evidence, populated from MEC status; assignment fields from campaign `scenarios.jsonl`. |
| `frames.csv` | One retained frame row, containing identity, outcome/reason, configuration, processing timestamps/durations, optional UE transmission/receipt measurements, and radio join information. | MEC `jobs.jsonl` and descriptor events; optional UE `transmitted`/`result_receipt` events; EdgeRIC radio reports for radio joins; campaign manifest/checkpoint for assignment and attempt context. |
| `frame_ran_features.csv` | One row per retained frame and associated radio measurement interval. Repeats frame/scenario fields and adds radio measurements and overlap duration. One frame can have several rows, or none if no interval matches. | `frames` plus `scenarios`, `frame_ran_windows`, and `ran_windows` in the generated database. Original measurements come from MEC/UE frame evidence and EdgeRIC `radio_report` events. |
| `frame_mec_features.csv` | One row per retained frame and associated MEC resource interval. Repeats frame/scenario fields and adds resource measurements and overlap duration. One frame can have several rows, or none if no interval matches. | `frames` plus `scenarios`, `frame_mec_windows`, and `mec_resources`. Resource measurements come from MEC `mec_resources` events. |
| `ran_windows.csv` | One UE's radio measurements for one report interval, retained if linked to a selected frame. Includes association validity, scheduler/radio measurements, traffic counters, and nested evidence. | EdgeRIC `events.jsonl`, specifically each UE entry in a `radio_report`. Selection depends on frame-to-window links. |
| `mec_resources.csv` | One MEC resource snapshot/interval retained if linked to a selected frame. Contains stream, worker, GPU, and original snapshot information. | MEC `events.jsonl`, specifically `mec_resources` events. Selection depends on frame-to-window links. |
| `validation.json` | One report object with validation status/problems, frame/join counts, window-selection counts, ingestion counts, and input file paths/sizes/hashes. `collection_window.attempts` has one summary per selected attempt. | Calculated from campaign checkpoint evidence, ingested raw records, and filtered database tables. Provenance includes the manifest lock when present and the input files actually scanned. |

The CSVs have one schema each; `completed`, `failed`, and `dropped` are different
values in the same frame schema, not different row layouts. Repeated frame rows
in the feature CSVs represent multiple matched measurement intervals, not
additional transmitted frames.

`videos.csv` outcome and transmission counts describe the **whole attempt**, not
just the exported five seconds. Manifest duration and frame-count fields are
requested/historical assignment metadata, not a count of retained frame rows.

In `validation.json`, `full_attempt_frame_rows` and ingestion `counts` describe
unfiltered accepted-attempt evidence. `frame_rows`, join statistics, and
`collection_window` describe the filtered dataset. Full-attempt validation
failures remain failures after filtering; `--strict` exits unsuccessfully if
validation fails. Excluding an untimestamped frame is reported separately and
does not by itself fail validation.

## Tables and views inside `dataset.sqlite3`

| Table/view | What one row represents | Origin |
| --- | --- | --- |
| `scenarios` | One manifest scenario with checkpoint metadata. | Campaign manifest/checkpoint; also exported as CSV. |
| `videos` | One accepted-attempt video/session with full-attempt counts. | Runner evidence plus manifest assignment; also exported as CSV. |
| `frames` | One selected terminal frame result with joined measurements. | MEC jobs plus supporting inputs; also exported as CSV. |
| `descriptors` | Received metadata for one selected frame. | MEC `descriptor` event. SQLite-only. |
| `ue_transmissions` | UE transmission evidence for one selected frame. | UE `transmitted` event. SQLite-only. |
| `ue_receipts` | UE receipt evidence for one selected frame result. | UE `result_receipt` event. SQLite-only. |
| `ran_windows` | One UE's radio measurements for an interval. | EdgeRIC `radio_report`; also exported as CSV. |
| `mec_resources` | One MEC resource snapshot/interval. | MEC `mec_resources` event; also exported as CSV. |
| `media_events` | One recognized MEC media diagnostic event within the selection window. | MEC event types listed above. SQLite-only; not joined into the frame-feature CSVs. |
| `frame_ran_windows` | A link between a selected frame and a radio interval, including join basis and overlap. | Exporter calculation from UE transmission time, MEC complete/last-packet reception time, and EdgeRIC interval bounds. SQLite-only. |
| `frame_mec_windows` | A link between a selected frame and a MEC resource interval, including overlap. | Exporter calculation from frame first-packet/result-ready timestamps and MEC resource interval bounds. SQLite-only. |
| `frame_ran_features` (view) | A selected frame combined with one associated radio interval and scenario metadata. | Joins the corresponding tables; also exported as CSV. |
| `frame_mec_features` (view) | A selected frame combined with one associated MEC resource interval and scenario metadata. | Joins the corresponding tables; also exported as CSV. |

Radio joins start at the UE transmission timestamp mapped to Unix time and end
at MEC complete reception. If the frame is incomplete, they end at the last
accepted RTP packet received for that frame (including duplicates before assembly
completion). MEC captures the reception timestamp in the packet callback and maps
it to Unix time there using `received_ns + time.time_ns() - time.monotonic_ns()`.
This is application-level reception timing, not a NIC hardware timestamp.

Frame stage timings retain `complete_reception_unix_ns`,
`last_packet_reception_monotonic_ns`, and `last_packet_reception_unix_ns` alongside
the existing completion monotonic timestamp. These fields and
`ran_join_end_unix_ns` are also columns in `frames` and its feature views/CSVs.
`ran_join_basis` distinguishes complete reception, last-packet reception, and
legacy descriptor-arrival endpoints. If the UE transmission timestamp is absent,
the start falls back to the MEC descriptor-event time. Older logs without Unix
reception endpoints retain the previous descriptor-arrival join. An end before
the start produces no radio links and is labeled `invalid_timestamp_order`.

An interval matches when the radio window ends after the frame interval starts
and starts before it ends. A point interval uses start-inclusive/end-exclusive
membership. The clocks on MEC and RAN must be synchronized; converting MEC clock
formats does not correct an inter-host offset. No offset is computed on the
exporter's machine. MEC resource joins continue using MEC monotonic timestamps,
from first packet to result ready.

The exporter can produce frame rows without optional UE logs or without matched
telemetry intervals. Missing measurements are not implicitly zero, and missing
receipt/SLO information must not be treated as successful delivery.

## Supplying source files

- `--campaign-dir`: directory containing `scenarios.jsonl` and `state.sqlite3`.
- `--mec-root`: MEC evidence directory scanned recursively for `events.jsonl`
  and `jobs.jsonl`.
- `--edgeric-root`: EdgeRIC evidence directory scanned recursively for
  `events.jsonl`.
- `--ue-log ALIAS=PATH`: repeat for each UE. A path can identify a copied JSONL
  file or a directory containing `events.jsonl` files.
- `--output`: destination for the database, CSVs, and validation report.

The exporter reads local paths; it does not fetch evidence from the devices.
Copy or mount the required MEC, RAN Spark, Pixel, and Orin evidence where the
exporter runs. Use the appropriate source directory for each input to avoid
mixing component event files. Original files should remain available for failure
diagnosis and provenance checks.

For collection lifecycle and the export command example, see
[data-collection-mode.md](data-collection-mode.md).
