# Dataset collection tutorial

This tutorial describes campaign setup using `multi-ue`. The **MEC
Spark** hosts the core network and MEC server. The **RAN Spark** hosts the gNB,
EdgeRIC, and campaign runner. The clients are three Pixel 7s and the Orin softUE.

## 1. Set a fresh MEC run ID

Before starting the MEC, edit `run_id` in
[`config/mec.collection.yaml`](../config/mec.collection.yaml). Use a unique,
descriptive value for this collection, for example:

```yaml
run_id: collection-<purpose>-<YYYYMMDD>
```

Ensure the configuration on the **MEC Spark** contains this value; editing the
RAN Spark's copy alone does not update the remote MEC. On each host, check with:

```bash
cd ~/mec-offload-ad/multi-ue
rg '^run_id:' config/mec.collection.yaml
```

The MEC writes evidence under `runs/<run_id>/mec/`. Preserve earlier run
directories. Keep this ID unchanged across retries and recovery within the
collection. The configuration is read at MEC startup; an already-running MEC
does not adopt the edit until restarted. Service startup is a later step.

The `run_id` identifies runtime evidence. The campaign YAML's `campaign_name`
separately becomes `campaign_id`; the manifest supplies `scenario_id`, and the
runner generates a new `attempt_id` for each attempt.

## 2. Create a campaign definition

Define the campaign on the **RAN Spark**. Use a new file in
[`config/campaigns/`](../config/campaigns/) for each experiment design. Start
from a schema-1 example supported by the current generator:

```bash
cd ~/mec-offload-ad/multi-ue
CAMPAIGN="my-campaign-v1"  # Replace with a unique descriptive name.
test ! -e "config/campaigns/${CAMPAIGN}.yaml" &&
  cp config/campaigns/aetheredge-pass-through-51prb-pilot.yaml \
     "config/campaigns/${CAMPAIGN}.yaml"
```

Edit the new file using the steps below. The copied example is only a starting
point: its channel settings enable pass-through emulation, and its media rule
may not match the intended comparison.

Set `campaign_name` to the chosen name and `campaign_seed` to an integer.
The name identifies the campaign; the seed makes media selection and randomized
execution reproducible. Set `execution.checkpoint_database` to
`runs/campaigns/<campaign_name>/state.sqlite3` for consistency; the runner uses
the directory supplied through `--campaign-dir`. Never reuse an existing
campaign directory for a changed design.

Only `schema_version: 1` is currently supported by
[`generate_campaign_manifest.py`](../tools/generate_campaign_manifest.py).
Do not copy schema-2 examples or add new YAML fields assuming they are
implemented. The following steps distinguish supported settings from limitations.

## 3. Choose UE sets

Choose which load levels to compare. A UE set is the list of devices active
simultaneously in one scenario. Use stable aliases matching the clients' alias
settings, not IP addresses or RNTIs. For example:

```yaml
ue_sets:
  one_ue: [pixel1]
  four_ue: [pixel1, pixel2, pixel3, linux1]
```

Keep only the sets needed for the experiment. To study contention, include both
a single-device baseline and a concurrent-device set. To study one device only,
keep one set. Every alias used here must appear in the top-level `ues` mapping.

The current generator's rotation validation requires `pixel1` and `linux1` have the same applications, and
all three applications in each rotation. Retaining the example yaml's four declared
UEs is the supported starting point even for a single-device experiment. The
media inventory must cover every declared UE, including inactive ones.

## 4. Choose application assignments

An `application_rotations` entry assigns an application to every declared UE.
Each entry is crossed with every UE set and tier combination. Choose rotations
so each device of interest runs every application being compared. Example:

```yaml
application_rotations:
  - {pixel1: detection, pixel2: segmentation, pixel3: tracking, linux1: detection}
  - {pixel1: tracking, pixel2: detection, pixel3: segmentation, linux1: tracking}
  - {pixel1: segmentation, pixel2: tracking, pixel3: detection, linux1: segmentation}
```

Keep the desired rotations; their list indices become rotation IDs. Only the
aliases in the selected UE set transmit. The current validator requires each
rotation to cover detection, tracking, and segmentation and requires `linux1`
to match `pixel1`. Other assignment patterns require generator changes.

Do not duplicate identical rotations to simulate repeated trials: rotations
represent application assignments, not repetition identifiers.

## 5. Choose model/bitrate tiers

First decide what the experiment compares:

- To compare fixed operating points, define one model/bitrate pair per tier.
- To exchange compute demand for radio demand, pair descending model sizes
  with ascending bitrates.
- To isolate model effects, hold bitrate constant while changing models.
- To isolate bitrate effects, hold models constant while changing bitrate.

Inspect the available candidates in [`config/tasks.yaml`](../config/tasks.yaml):

```bash
cd ~/mec-offload-ad/multi-ue
../gnb-core/env/bin/python - <<'PYTHON'
import yaml
from pathlib import Path
catalog = yaml.safe_load(Path('config/tasks.yaml').read_text())
print('application,model,bitrate_bps,candidate_id')
for item in catalog['configurations']:
    print(','.join(str(item[k]) for k in
                   ('application', 'model', 'bitrate_bps', 'id')))
PYTHON
```

Choose exact catalog combinations; the generator rejects missing candidates.
Adding a candidate also requires its model artifact and compatible MEC
configuration/profile support, not just a new campaign label.

For size-ordered pairings, the current model families are:

| Size order | Detection/tracking | Segmentation |
|---|---|---|
| Largest | `yolo26x` | `segformer-b3` |
| Next largest | `yolo26l` | `segformer-b2` |
| Next smallest | `yolo26m` | `segformer-b1` |
| Smallest | `yolo26n` | `segformer-b0` |

Write a `tiers` list numbered consecutively from zero. Each tier must contain
`bitrate_bps`, `detection`, `tracking`, and `segmentation`, even if a selected
UE set uses only one application. One example entry is:

```yaml
tiers:
  - tier: 0
    bitrate_bps: 500000
    detection: yolo26x
    tracking: yolo26x
    segmentation: segformer-b3
```

Add entries for the remaining selected pairs. Rates are encoded-video targets
in bits per second: multiply Mbit/s by 1,000,000. Achieved throughput can differ.
Do not infer bitrate values from model names or copy a different experiment's
rate ladder without checking it.

Set `tier_assignment` explicitly:

- `shared`: all active UEs use the same tier index in a scenario. Each UE uses
  that tier's model for its assigned application.
- `per_ue`: enumerate every independent tier combination across active UEs.
  This grows exponentially with the number of UEs.

## 6. Choose radio conditions

For observed physical RF without channel emulation, replace the entire
`channels` section with:

```yaml
channels:
  enabled: false
```

This creates one condition named `physical-rf`. The deployed gNB must also
have emulation disabled; campaign YAML does not switch it off. Check startup
logs for `Channel emulation disabled`, no `CIR ZMQ initialized`, and no listener
on emulator port 5560. Record device placement and RF settings with the run;
physical RF observations are not controlled synthetic SINR labels.

For emulator experiments, follow
[`channel-emulation.md`](channel-emulation.md) and use the channel fields in
[`aetheredge-fixed-policy-51prb-v1.yaml`](../config/campaigns/aetheredge-fixed-policy-51prb-v1.yaml).
Choose models, SINRs, delay spreads, and speeds deliberately, then prepare and
validate the corresponding trace bank. Pass-through (`pass_through_only: true`)
is an emulator condition and is different from disabling emulation.

Verify `radio` parameters against the deployed gNB: carrier PRBs, frequency,
subcarrier spacing, FFT size, sample rate, and sampling mode. These fields label
the experiment; they do not configure the radio. Keep the fixed collection
policies unless changing them is an explicitly supported experiment:
proportional-fair RAN scheduling, EdgeRIC shadow mode, MEC collection mode,
FIFO per application, and CUDA priority `-3`.

## 7. Choose repetitions and calculate scenario count

A repetition is another planned observation of the same experimental
combination. A retry replaces an unsuccessful attempt; it is not an independent
planned repetition. Choose repetitions based on the variability to measure,
then budget time for preparation, transmission, draining, and recovery.

**Current limitation:** the generator implements one planned observation per
combination. It has no supported repetition field. Adding
`repetitions.trials_per_combination` does not implement repeated trials. For
multiple planned observations, use separately named campaigns with preserved
manifests and record their relationship, or extend the generator and scenario
identity to include a trial index before generation. The runner skips valid
scenarios, so rerunning it does not collect repetitions.

Let C be the number of radio conditions, A the number of application rotations,
T the number of tiers, and S the selected UE sets. For one campaign:

| Tier assignment | Scenario count |
|---|---|
| Shared | `C × A × T × number_of_UE_sets` |
| Per UE | `C × A × sum(T ** len(ue_set) for ue_set in S)` |

For physical RF or emulator pass-through, C = 1. With AWGN and statistical
channels, C is the number of AWGN SINRs plus
`number_of_statistical_models × number_of_SINRs × number_of_delay_spreads × number_of_speeds`.

For example, one physical condition, three rotations, four shared tiers, and
two UE sets produce 24 scenarios. This is a counting example, not a required
design. Set `execution.expected_scenarios` to the calculated count; generation
fails if it disagrees. A scenario with four active UEs is still one scenario.

Set `execution.randomize_order: true` to spread time-dependent effects across
conditions, or `false` for deterministic nested-loop order during debugging.
Randomization uses `campaign_seed` and changes execution order, not coverage.
Do not use runner `--limit` as a substitute for a designed subset: it takes a
manifest prefix, which may not balance the factors.

## 8. Choose the media comparison rule

Before choosing filenames, decide which comparisons must hold source content
constant. Define a segment by source SHA-256 and starting frame. Record whether
segments should be reused across applications, tiers, UE sets, radio
conditions, and devices. For example:

| Intended comparison | Hold constant |
|---|---|
| Applications at a fixed tier | Segment for a given UE and tier |
| Tiers for a fixed application | Segment for a given UE and application rotation |
| Single-UE versus concurrent load | Participating baseline UE's segment in both sets |
| Different radio conditions | Segment for the same UE/application/tier |

A mapping that assigns segment S0 to tier 0, S1 to tier 1, etc., and repeats
that mapping across applications controls content for application comparisons.
It changes content between tiers, so tier effects and content effects are
mixed. Choose the reuse rule to match the experiment's question.

Check support before preparing the media:

- With `tier_assignment: shared`, the current generator selects a segment by
  channel condition, application rotation, and UE, and reuses it across tiers
  and UE sets. It does **not** implement selection by UE/tier with reuse across
  applications.
- With `tier_assignment: per_ue`, it cycles through a seeded per-UE segment list
  as assignments are generated. This does not guarantee paired content across
  applications or UE sets.
- `media.selection` is descriptive in the current generator; editing that text
  does not change the selection algorithm. Unsupported reuse rules require
  implementation and verification before manifest generation.

Choose `media.videos_per_ue`, `segments_per_video`, and
`frames_per_scenario` together with the media inventory. Every declared UE
must have exactly `videos_per_ue` inventory entries; each file must have at least
`segments_per_video × frames_per_scenario` frames. Shared-tier selection needs
at least C × A available segments per UE. These are generator requirements,
not proof that a selected segment is long enough for runtime transmission.

The MEC currently issues stop seven seconds after release and then drains.
Export selects arrivals in `[1 s, 6 s)` relative to MEC release. `duration_s`
and the manifest's historical frame count do not change this timer. Sources
must have enough remaining video for the entire transmission plus stop-command
delay; a 150-frame extract is insufficient at 30 FPS. Reusing source content
cannot guarantee identical received or exported frames.

## 9. Review the design before preparing files

Before moving to video preparation and manifest generation, ensure the campaign
YAML records the name/seed, UE sets, application rotations, exact model/bitrate
pairs, tier assignment, channel settings, radio profile, scenario count, and
execution order. Record the intended repetition and media-reuse rules in YAML
comments when they have no supported executable field; comments do not enable
them. Resolve unsupported requirements before proceeding.

After generation, verify the manifest against those decisions: counts per UE
set and rotation, candidate IDs/rates, channel labels, and source hashes/start
frames for every intended paired comparison. Preserve the manifest, lock file,
and checkpoint after collection begins. Do not regenerate them to change an
in-progress experiment.

The next setup stage is to prepare source videos and their per-device inventory,
then generate and inspect the immutable manifest. Device deployment and service
startup follow that validation.

## 10. Select and verify the source-media inventory

Choose whether to reuse an existing media pool or prepare a new one. Reuse is
appropriate when the existing videos match the experiment's content requirements.
The inventory lists available files; the campaign seed and selection algorithm
choose the actual assignments during manifest generation.

For an existing pool, select its inventory YAML and local source directory.
For example, `config/campaigns/media-inventory-v2.yaml` corresponds to
`datasets/campaign-v2`. Run this on the **RAN Spark**, changing the two paths
when using another pool:

```bash
cd ~/mec-offload-ad/multi-ue
../gnb-core/env/bin/python - <<'PY'
import hashlib
from pathlib import Path
import yaml

inventory = yaml.safe_load(Path('config/campaigns/media-inventory-v2.yaml').read_text())
source = Path('datasets/campaign-v2')
verified = {}
for alias, assets in inventory['pools'].items():
    print(alias, len(assets), 'inventory entries')
    for asset in assets:
        path = source / Path(asset['path']).name
        if path not in verified:
            with path.open('rb') as stream:
                verified[path] = hashlib.file_digest(stream, 'sha256').hexdigest()
        if verified[path] != asset['sha256']:
            raise SystemExit(f'Hash mismatch: {alias}: {path}')
print(f'Verified {len(verified)} unique local files')
PY
```

Require the inventory count for each declared UE to match `videos_per_ue`.
Check that inventory paths describe the intended device destinations: Android
paths refer to files in the selected document-tree directory; Linux paths must
exist inside the softUE container. Local hash verification does not verify
copies installed on remote devices; check those during deployment.

For a new pool, inspect the preparation tool's options before staging files into
a new directory and writing a new inventory:

```bash
../gnb-core/env/bin/python tools/prepare_campaign_media.py --help
```

Use `--source-dir` or `--archive`, a fresh `--output` and `--inventory-output`,
and explicit `--count`, `--seed`, `--segment-frames`, and
`--segments-per-video` matching the intended pool. Set `--android-prefix` and
`--linux-prefix` to the deployment destinations. Use `--decode-check` and
`--random-access-check` to check decoding and segment access. Preserve the
generated inventory and hashes. These checks do not establish sufficient
runtime duration after every selected starting position.

For beginning-of-file selection, use `segments_per_video: 1`; the starting
frame will be zero. This also avoids a current cross-client difference:
Android converts nonzero starting frames to timestamps using configured output
FPS, while Linux seeks by source frame index. Do not assume nonzero offsets
refer to identical content when source FPS differs from output FPS.

Next, generate the manifest in a fresh campaign directory and inspect the
selected files and starting positions. Verify each selected source has enough
playable duration for the seven-second transmission plus stop-command delay
before deploying and running it.

## 11. Generate the immutable manifest

On the **RAN Spark**, set the campaign name and inventory path to the files
chosen above. These shell variables are reused in the inspection commands:

```bash
cd ~/mec-offload-ad/multi-ue
CAMPAIGN="my-campaign-v1"  # Replace with your campaign_name.
INVENTORY="config/campaigns/media-inventory-v2.yaml"
CAMPAIGN_DIR="runs/campaigns/${CAMPAIGN}"

if [ -e "$CAMPAIGN_DIR" ]; then
  echo "Output already exists; inspect it instead of regenerating: $CAMPAIGN_DIR"
else
  ../gnb-core/env/bin/python tools/generate_campaign_manifest.py \
    --campaign "config/campaigns/${CAMPAIGN}.yaml" \
    --catalog config/tasks.yaml \
    --media-inventory "$INVENTORY" \
    --output "$CAMPAIGN_DIR"
fi
```

Generation creates [`scenarios.jsonl`](../runs/campaigns/) (one scenario per
line) and `manifest.lock.json` (input/manifest hashes and counts) inside the
chosen directory. It does not start services, transmit video, or create the
runner's checkpoint database. The generator itself can overwrite output, so
keep the directory-existence guard. After an interruption, inspect any existing
output first. Never overwrite an in-progress or completed campaign.

## 12. Inspect and verify the manifest

Use the same shell variables as step 11. If opening a new terminal, set them
again. First check hashes, deterministic expansion, and counts, then print all
assignments for review:

```bash
../gnb-core/env/bin/python - "$CAMPAIGN" "$INVENTORY" "$CAMPAIGN_DIR" <<'PY'
import hashlib
import json
import runpy
import sys
from collections import Counter, defaultdict
from pathlib import Path

name, inventory_path, output = sys.argv[1:]
root = Path(output)
campaign_path = Path('config/campaigns') / f'{name}.yaml'
catalog_path = Path('config/tasks.yaml')
manifest_path = root / 'scenarios.jsonl'
lock = json.loads((root / 'manifest.lock.json').read_text())
for field, path in (
    ('campaign_sha256', campaign_path),
    ('catalog_sha256', catalog_path),
    ('media_inventory_sha256', Path(inventory_path)),
    ('scenarios_sha256', manifest_path),
):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != lock[field]:
        raise SystemExit(f'FAIL: {path} differs from the locked input/output')

# Rebuild in memory only; do not invoke the generator's file-writing main().
generator = runpy.run_path('tools/generate_campaign_manifest.py')
config = generator['load_yaml'](campaign_path)
catalog = generator['load_yaml'](catalog_path)
inventory = generator['load_media'](inventory_path, config)
rows = [json.loads(line) for line in manifest_path.read_text().splitlines() if line.strip()]
expected = generator['build'](config, catalog, inventory)
if rows != expected:
    raise SystemExit('FAIL: manifest differs from current deterministic expansion')
if name != config['campaign_name'] or name != lock['campaign_id']:
    raise SystemExit('FAIL: campaign name/path mismatch')
if len(rows) != lock['scenario_count']:
    raise SystemExit('FAIL: scenario count differs from lock')
if len({row['scenario_id'] for row in rows}) != len(rows):
    raise SystemExit('FAIL: duplicate scenario IDs')

print('PASS: input/output hashes, expansion, campaign name, and unique IDs')
print('Total scenarios:', len(rows))
print('By UE set:', dict(Counter(row['factors']['ue_set'] for row in rows)))
print('By rotation:', dict(Counter(row['factors']['application_rotation'] for row in rows)))
print('By channel:', dict(Counter(row['channel']['condition_id'] for row in rows)))
print('Radio:', config.get('radio'))
print('FPS / GoP:', config['fps'], config['gop_frames'])
print('\nExecution order and assignments:')
media = defaultdict(set)
for row in rows:
    factors = row['factors']
    print(row['execution_index'], row['scenario_id'], factors['ue_set'],
          'rotation=', factors['application_rotation'])
    for ue in row['ues']:
        print(' ', ue['ue_alias'], ue['application'], 'tier=', ue['tier'],
              ue['candidate_id'], 'bps=', ue['bitrate_bps'],
              'file=', Path(ue['media_path']).name,
              'start_frame=', ue['source_start_frame'])
        media[(ue['ue_alias'], factors['application_rotation'])].add(
            (ue['media_sha256'], ue['source_start_frame']))
if config.get('tier_assignment', 'shared') == 'shared' and len(
    {row['channel']['condition_id'] for row in rows}
) == 1:
    if any(len(segments) != 1 for segments in media.values()):
        raise SystemExit('FAIL: shared-tier media reuse differs across tiers/UE sets')
    print('\nPASS: one segment per UE/rotation, reused across tiers and UE sets')
PY
```

Successful automatic checks establish that the manifest matches its inputs and
the current generator. Also review the printed assignments against the intended
experiment: total/per-set counts, model/bitrate pairs, applications, channel
condition, FPS/GoP, and video starting frames. For physical RF, every row must
use `physical-rf`; verify `emulation_enabled: false` in `scenarios.jsonl`.
Check FPS/GoP against the deployed MEC configuration separately.

Inspect a complete scenario or the lock file without special tooling:

```bash
head -n 1 "$CAMPAIGN_DIR/scenarios.jsonl" | python3 -m json.tool
python3 -m json.tool "$CAMPAIGN_DIR/manifest.lock.json"
```

For each selected source, inspect duration using its local path:

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=duration,avg_frame_rate,nb_frames \
  -of json datasets/campaign-v2/SELECTED_FILENAME.mov
```

Replace the filename with one printed above. Require enough remaining playable
duration after the selected start for seven seconds plus stop-command delay.
Hash/metadata checks do not replace decode validation or a live client check.
If an input or assignment is wrong, correct it and generate into a fresh,
uniquely named campaign directory. Do not manually edit the manifest or lock.

Once this review passes, preserve both generated files. The next stage is
verifying/staging the selected media on the devices and checking deployed
configuration, images, and services before running scenarios.

## 13. Validate the deployed MEC configuration and image

On the **MEC Spark**, confirm that the synchronized configuration loads inside
the actual MEC image. This starts a temporary validation container, not the MEC
service; it does not require GPU access or start inference workers.

```bash
cd ~/mec-offload-ad/multi-ue

docker run --rm -i --network none \
  -v "$PWD/config:/work/multi-ue/config:ro" \
  -v "$PWD/weights:/work/multi-ue/weights:ro" \
  --entrypoint python multi-ue-mec:baseline - <<'PY'
from pathlib import Path
from mec_server.config import load

settings, catalog, profiles = load('config/mec.collection.yaml')
print('Configuration loaded successfully')
print('run_id:', settings.run_id)
print('mode:', settings.mode)
print('FPS / GoP:', settings.fps, settings.gop_frames)
print('profile compatibility:', profiles.get('compatibility'))
by_id = {item['id']: item for item in catalog['configurations']}
for application, candidate_id in settings.fixed_candidates.items():
    artifact = Path(settings.weights_dir) / by_id[candidate_id]['artifact']
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise SystemExit(f'Missing or empty model artifact: {artifact}')
    print(application, candidate_id, artifact, 'present')
PY
```

Require collection mode and the campaign's FPS/GoP and run ID. A
`catalog FPS/GoP must match MEC settings` error means the deployed catalog and
MEC configuration still disagree. Do not bypass this validation. If a catalog
or campaign input changes after manifest generation, preserve the old output
and regenerate/review the manifest before collection.

The check above validates startup defaults and artifact presence, not model
loading, GPU execution, or every candidate selected by the campaign. Those
require further checks before running scenarios. Existing measured profiles
retain their original measurement provenance; changing FPS/GoP does not
reprofile encoding or inference.

The Docker image embeds Python code, while Compose mounts configuration from
the host. Compare key collection-control files against the MEC host source:

```bash
sha256sum mec/src/mec_server/{app,engine,config}.py

docker run --rm --network none --entrypoint sha256sum \
  multi-ue-mec:baseline \
  mec/src/mec_server/app.py \
  mec/src/mec_server/engine.py \
  mec/src/mec_server/config.py
```

Each file's hash must match between host and image. These checks cover the
listed files only; also ensure the MEC host has the intended source revision.
If the image is stale, rebuild from that source and repeat validation:

```bash
./scripts/containers.sh build-mec
```

Do not start a campaign until configuration validation succeeds and the image
contains the intended implementation. Next check all campaign model artifacts,
GPU/MPS readiness, and the core/RAN services before starting collection.

## 14. Rebuild collection profiles when candidate coverage is missing

The `profile_compatibility` value is a matching label, not a request to load
the latest measurements. Inspect `provenance.sources` and `candidates` in the
configured profile file. Every campaign candidate needs measured processing
time, I/P-frame sizes, and (for tracking) tracker time.

The collection-profile builder permits missing detection/segmentation quality
rows. It records `quality_status: not_measured`, omits scores, and lists these
candidates under `provenance.missing_quality_candidates`. Existing quality
measurements and their input hashes are preserved. Tracking retains its
separate unmeasured-HOTA status. All candidates remain `qualified: false`, so
these profiles cannot enable heuristic scheduling. Missing execution evidence
or malformed supplied quality measurements still cause an error.

Choose measured inputs explicitly. Encoding measurements must describe the
intended FPS/GoP and encoder. The current builder accepts one frame-size CSV
and does not distinguish devices: using Pixel measurements makes them shared
initialization estimates, not measured Linux estimates. Do not silently combine
different encoder datasets. Quality CSVs remain required arguments, but they
need not contain every candidate's bitrate. Do not substitute quality scores
from another bitrate or label historical measurements as newly measured.

After deploying the updated builder to the **MEC Spark**, rebuild into a new
file. Set these variables to the measured input files selected for the run:

```bash
cd ~/mec-offload-ad/multi-ue
PROCESSING="runs/profiling/<CHOSEN_MODEL_TIMING>/processing.csv"
FRAME_SIZES="runs/profiling/<CHOSEN_ENCODER>/frame-sizes.csv"
DETECTION_QUALITY="runs/profiling/<CHOSEN_DETECTION_QUALITY>/summary.csv"
SEGMENTATION_QUALITY="runs/profiling/<CHOSEN_SEGMENTATION_QUALITY>/summary.csv"
NEW_PROFILE="config/profiles.collection-new.yaml"

if [ -e "$NEW_PROFILE" ]; then
  echo "Choose a new output filename; existing profile retained."
else
  docker run --rm --network none \
    -v "$PWD:/work/multi-ue:ro" \
    -v "$PWD/config:/work/multi-ue/config" \
    --entrypoint python multi-ue-mec:baseline \
    tools/build_collection_profiles.py \
    --catalog config/tasks.yaml \
    --processing "$PROCESSING" \
    --frame-sizes "$FRAME_SIZES" \
    --detection-quality "$DETECTION_QUALITY" \
    --segmentation-quality "$SEGMENTATION_QUALITY" \
    --priority -3 \
    --output "$NEW_PROFILE"
fi
```

Review the reported missing-quality candidates and the new file's provenance.
Set `profiles` in `mec.collection.yaml` to the new filename only after verifying
coverage for every candidate in the campaign, and repeat step 13. Keep the
old profile file. The manifest lock does not hash this profile file, so a
profile-only update does not require regenerating the manifest; preserve the
chosen profile and its hash with run evidence. Catalog or campaign changes
still require a new manifest review.

## 15. Preserve the validated configuration and check GPU access

After validating all catalog candidates and model artifacts on the MEC Spark,
copy the selected profile and MEC configuration back to the RAN Spark so both
hosts retain the configuration actually used. On the **RAN Spark**, set the
profile filename to the one selected in `mec.collection.yaml`:

```bash
cd ~/mec-offload-ad/multi-ue
MEC_HOST=sstreit@192.168.71.130
PROFILE=profiles.collection.yaml
scp "$MEC_HOST:~/mec-offload-ad/multi-ue/config/$PROFILE" config/
scp "$MEC_HOST:~/mec-offload-ad/multi-ue/config/mec.collection.yaml" config/
```

Preserve copies and hashes with the campaign evidence. Set `CAMPAIGN` to the
campaign name before running this on the **RAN Spark**:

```bash
EVIDENCE="runs/campaigns/$CAMPAIGN/deployment"
if [ -e "$EVIDENCE" ]; then
  echo "Deployment evidence already exists; preserve it and use a new snapshot directory."
else
  mkdir -p "$EVIDENCE"
  cp config/mec.collection.yaml "config/$PROFILE" "$EVIDENCE/"
  (cd "$EVIDENCE" && sha256sum mec.collection.yaml "$PROFILE" > SHA256SUMS)
fi
```

On the **MEC Spark**, check the GPU and CUDA visibility inside the deployed
image, then inspect running services:

```bash
cd ~/mec-offload-ad/multi-ue
nvidia-smi
./scripts/containers.sh gpu
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

Require CUDA availability to be `True` and the expected GPU to be identified.
The GPU helper uses a temporary container and does not start MEC workers or
validate inference. Inspect the existing core containers separately; an empty
container list means the core still needs startup. Next prepare the core and
MPS, then start the MEC and check readiness before connecting clients.

## 16. Start MEC MPS and the collection service

On the **MEC Spark**, first ensure the existing core-network stack is running
and its external Docker networks are available. The commands below manage only
the MEC additions; they do not launch the core stack.

```bash
cd ~/mec-offload-ad/multi-ue
docker network inspect oai-traffic-net oai-public-net --format '{{.Name}}'
./scripts/containers.sh start-mps
MEC_CONFIG=mec.collection.yaml ./scripts/containers.sh start-mec
```

Run the commands in order and stop on any error. `start-mps` may request sudo
authentication. It manages the MEC Spark's MPS daemon, separately from the RAN
Spark's daemon. Do not stop or repair MPS while a running MEC is using it.

Check container state and readiness:

```bash
docker inspect multi-ue-mec \
  --format 'status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'

docker exec -i multi-ue-mec python - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=10) as response:
    print(json.dumps(json.load(response), indent=2))
PY
```

Model loading and warmup can take time. If startup is still in progress, inspect
logs and repeat the readiness check after loading completes:

```bash
docker logs --tail 100 multi-ue-mec
```

Require `status=running`, `oom=false`, readiness `ready: true`, and mode
`collection`. If the container exits or readiness reports an error, resolve it
before proceeding; do not repeatedly restart without inspecting logs. Keep
startup evidence with the campaign. MEC readiness alone does not establish
RAN telemetry, client associations, or end-to-end media delivery. Those are
checked next, before any scenario is armed.

## 17. Verify the gNB and start EdgeRIC in shadow mode

On the **RAN Spark**, check the gNB before starting EdgeRIC:

```bash
cd ~/mec-offload-ad/multi-ue
docker inspect oai-gnb --format 'status={{.State.Status}}'
docker logs oai-gnb 2>&1 | \
  rg 'Channel emulation disabled|CIR ZMQ initialized|ofdm_symbol_size|sample_rate' | tail -30
rg '^mode:' config/edgeric.shadow.yaml
```

Require a running gNB. If it is absent or stopped, launch it through the
testbed's existing SRK workflow before proceeding. For a physical-RF campaign,
check the current gNB startup for disabled channel emulation and matching radio
parameters. Old log entries are not proof of the current process configuration.
Require `mode: shadow` and ensure no other scheduling application publishes
weights. Shadow mode records proposed weights without sending them to OAI.

With the intended EdgeRIC image built and the gNB running:

```bash
./scripts/containers.sh start-edgeric
docker inspect edgeric-mec --format 'status={{.State.Status}}'
docker logs --tail 60 edgeric-mec
```

EdgeRIC shares the gNB network namespace. Recreate EdgeRIC after recreating
the gNB so it attaches to the new namespace.

On the **MEC Spark**, check readiness and radio-report freshness:

```bash
docker exec -i multi-ue-mec python - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=10) as response:
    status = json.load(response)
print('readiness:', json.dumps(status.get('readiness'), indent=2))
print('radio_report_age_s:', status.get('radio_report_age_s'))
print('unresolved:', status.get('unresolved'))
print('ues:', json.dumps(status.get('ues', {}), indent=2))
PY
```

Require fresh reports (a non-null `radio_report_age_s` below one second).
If reports are absent or stale, inspect EdgeRIC/gNB logs and MEC-to-EdgeRIC
connectivity before connecting clients. An empty UE registry is expected before
clients connect. The next step is preparing client aliases and dataset paths,
then verifying every participating UE's connection and radio association.

## 18. Connect clients and verify radio associations

### Connect and associate clients

Prepare all clients required anywhere in the manifest before starting a
randomized campaign. Connected clients that are not assigned to a scenario
wait; the manifest determines which clients transmit.

On each **Pixel**, open the installed campaign app, set the MEC address to
`192.168.72.136`, and set its unique alias to the corresponding `ues` key
(`pixel1`, `pixel2`, or `pixel3` on this testbed). Select the dataset directory
whose files match the inventory. For the existing V2 pool, choose
`Movies/AetheredgeCampaignV2` and verify the displayed 42-video count. Press
**Connect and stream** once and wait for **MEC control connected**. Keep the
app in the foreground. Campaign assignments select application, model, bitrate,
and media; do not manually restart streaming between scenarios.

On the **Orin**, check the already-attached softUE and its container mounts:

```bash
cd ~/mec-offload-ad/multi-ue
docker inspect oai-nr-ue --format 'status={{.State.Status}}'
docker exec oai-nr-ue ip -4 addr show oaitun_ue1
docker exec oai-nr-ue python3 -c \
  'from pathlib import Path; p=Path("/datasets/campaign-v2"); print("dataset exists:", p.is_dir()); print("videos:", len(list(p.glob("*.mov")))); print("client mounted:", Path("/multi-ue/ue/linux/src/mec_ue/client.py").is_file())'
```

Replace `/datasets/campaign-v2` if the chosen inventory uses a different path.
Require the tunnel IPv4 address, the expected video count, and mounted client
code. If anything is missing, fix attachment or mounts before starting the
client. See [`docker-compose.ue-mec.yaml`](../../ue/config/common/docker-compose.ue-mec.yaml)
for the source, dataset, and writable log mounts. Device file counts alone do
not establish byte-for-byte agreement with inventory hashes.

### Check and restart the Orin client for the current campaign

Before launching a client, check whether recovery or an earlier session already
started one. Run these commands on the **Orin**:

```bash
cd ~/mec-offload-ad/multi-ue
pgrep -af '[s]oftue-client-supervisor.sh' || true
docker top oai-nr-ue -eo pid,args
if [ -f runs/softue-client-launch.json ]; then
  python3 -m json.tool runs/softue-client-launch.json
fi
```

In `docker top`, look for `python3 -m mec_ue.client`; its `--logs` argument shows
where the running client writes evidence. The `nr-uesoftmodem` processes are
the cellular UE, not the MEC application client. The launch JSON records the
last supervisor arguments for recovery; its existence alone does not prove
that a client is running.

If the client is already using the intended campaign directory, leave it
running and proceed to the MEC association check below. If neither the client
nor supervisor is running, skip termination and follow the identity guidance
below before launching.
Otherwise, use the following stop/restart sequence to change directories.

To stop and restart only the Linux application client (for a deployment update
or a new campaign log directory), first stop the campaign runner and wait for
its cleanup to finish. In the **Orin** supervisor terminal, press **Ctrl+C** and
wait for the shell prompt. Leave the OAI `oai-nr-ue` container running.

If that terminal is unavailable, run this from another **Orin** terminal. Stop
the supervisor first so it cannot relaunch the client, then terminate only the
`mec_ue.client` process inside the container:

```bash
cd ~/mec-offload-ad/multi-ue
pgrep -af '[s]oftue-client-supervisor.sh'
pkill -TERM -f '[s]oftue-client-supervisor.sh' || true

docker exec -i oai-nr-ue python3 - <<'PYTHON'
import os
import signal
from pathlib import Path

for process in Path('/proc').iterdir():
    if not process.name.isdigit():
        continue
    try:
        args = (process / 'cmdline').read_bytes().split(b'\0')
        if b'-m' in args and args[args.index(b'-m') + 1] == b'mec_ue.client':
            os.kill(int(process.name), signal.SIGTERM)
            print('Stopped client PID', process.name)
    except (OSError, ValueError, IndexError):
        continue
PYTHON
```

Before restarting, verify that the supervisor and client have exited:

```bash
pgrep -af '[s]oftue-client-supervisor.sh'
docker top oai-nr-ue -eo pid,args
flock -n "${SOFTUE_CLIENT_LOCK:-/tmp/aetheredge-softue-client.lock}" \
  -c 'echo "Supervisor lock is free"'
```

Require no supervisor process, no `python3 -m mec_ue.client` process, and the
lock-free message. The OAI softmodem processes should remain. If the lock is
still held, wait for shutdown and repeat these checks; do not delete the lock
file or start a second supervisor.

**For a new campaign:** the Linux client's `identity.json` lives in its log
directory. A new directory normally has no saved resume token. After stopping
the old Orin client, restart the MEC with the new campaign configuration and
`run_id` before launching the new client. This clears the MEC's in-memory UE
records, including any disconnected record reserving `linux1`. If the MEC was
already restarted and the old client never reconnected, another restart is
unnecessary. This MEC action runs on the **MEC Spark**, not on the Orin. Other
UEs must reconnect and reestablish associations after it. Do not delete old
campaign identities or copy them into the new directory for this workflow.

For a restart within the **same campaign**, keep the same log directory and its
identity file; do not restart the MEC solely to restart the client.

Start in a dedicated **Orin** terminal with the current campaign's log path:

```bash
cd ~/mec-offload-ad/multi-ue
CAMPAIGN=my-new-campaign  # Replace with the campaign directory name you are starting.
./scripts/softue-client-supervisor.sh \
  --ue-alias linux1 \
  --source-dir /datasets/campaign-v2 \
  --logs "/multi-ue/runs/$CAMPAIGN/ue/linux1" \
  --preview-port 0
```

The log path is inside the container and must map to writable host storage.
Preview is disabled to avoid extra work during collection. The client waits
for MEC scenario assignments and start/stop commands; launching it does not
arm a scenario.

Use the same `CAMPAIGN` when resuming a campaign, or the new directory name
when collecting a fresh repeat. Replace the source directory if needed. This
launch updates [`runs/softue-client-launch.json`](../runs/softue-client-launch.json),
which recovery uses to restore the exact arguments; existing logs are retained.
Keep the terminal open. The launcher is
[`softue-client-supervisor.sh`](../scripts/softue-client-supervisor.sh), which
invokes [`softue-client.sh`](../scripts/softue-client.sh).

In a second **Orin** terminal, confirm that the new launch is running and that
both the process arguments and saved recovery arguments contain the intended
`--logs` path:

```bash
cd ~/mec-offload-ad/multi-ue
pgrep -af '[s]oftue-client-supervisor.sh' || true
docker top oai-nr-ue -eo pid,args
python3 -m json.tool runs/softue-client-launch.json
```

On the **MEC Spark**, check connections and associations:

```bash
docker exec -i multi-ue-mec python - <<'PY'
import json
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=10) as response:
    status = json.load(response)
print('radio_report_age_s:', status.get('radio_report_age_s'))
print('unresolved:', status.get('unresolved'))
for uid, ue in status.get('ues', {}).items():
    print(uid, 'alias=', ue.get('alias'), 'connected=', ue.get('connected'),
          'last_control_age_s=', ue.get('last_control_age_s'),
          'control_timeout_s=', ue.get('control_timeout_s'),
          'association_valid=', ue.get('association_valid'), 'ip=', ue.get('ip'))
PY
```

Require each intended alias to report `connected=True` and
`association_valid=True`, with fresh radio reports and `unresolved=0` before
arming the first scenario. Probing is serialized, so associations may take
several seconds. Duplicate aliases, missing clients, or persistently invalid
associations must be resolved first. Next verify runner recovery access and
select one scenario for the initial end-to-end collection check.

The MEC expires a control connection after five seconds without incoming control
messages in collection mode, including while idle (thirty seconds in other modes).
`last_control_age_s` shows time since the last incoming control message;
`control_timeout_s` shows the applicable limit. Both clients normally send messages
every second in collection mode. A timeout clears `connected`, closes the stale
socket, and records a `control_timeout` event. Restore cellular connectivity and
wait for the client to reconnect and regain a valid association before proceeding.
During a started scenario, the existing two-second control-silence failure check
still applies.

## 19. Verify runner recovery access

Run these checks on the **RAN Spark**, in the terminal that will run the
campaign. Substitute the actual campaign name and SSH destinations:

```bash
cd ~/mec-offload-ad/multi-ue
CAMPAIGN=my-campaign-v1
export AETHEREDGE_CAMPAIGN_DIR="$PWD/runs/campaigns/$CAMPAIGN"
export AETHEREDGE_MEC_HOST=sstreit@192.168.71.130
export AETHEREDGE_ORIN_HOST=srk@130.127.199.100

ssh -o BatchMode=yes -o ConnectTimeout=5 "$AETHEREDGE_MEC_HOST" \
  'hostname; docker inspect multi-ue-mec --format "{{.State.Running}}"'

ssh -o BatchMode=yes -o ConnectTimeout=5 "$AETHEREDGE_ORIN_HOST" \
  'hostname; docker inspect oai-nr-ue --format "{{.State.Running}}"; test -r ~/mec-offload-ad/multi-ue/runs/softue-client-launch.json && echo supervisor-launch-record-present'

curl --fail --silent --show-error --max-time 10 \
  http://192.168.71.142:8765/readyz | python3 -m json.tool
```

Both SSH checks must work without interactive authentication, Docker access
must succeed, and the MEC control-side readiness endpoint must report ready.
The normal Orin supervisor writes the launch record that preserves the source,
alias, and log arguments during recovery. If authentication fails, fix SSH
access or set the correct destination before running. The earlier client
association check does not establish SSH access.

Do not invoke `campaign_recover.sh` as a connectivity test: it may restart
services. These commands only inspect access and running state. Keep these
environment variables in the runner's shell so recovery uses the intended
hosts and campaign evidence directory.

## 20. Run and validate one initial scenario

Use the **RAN Spark** terminal with the recovery environment variables and,
if needed, SSH agent loaded. Keep the Pixel apps foregrounded and the Orin
supervisor running. Set `CAMPAIGN` to the campaign being collected. Select one
pending single-UE scenario from the immutable manifest:

```bash
cd ~/mec-offload-ad/multi-ue
CAMPAIGN=my-campaign-v1
export AETHEREDGE_CAMPAIGN_DIR="$PWD/runs/campaigns/$CAMPAIGN"

SCENARIO_ID="$(python3 - "$AETHEREDGE_CAMPAIGN_DIR" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
states = {}
database = root / 'state.sqlite3'
if database.exists():
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        states = dict(db.execute('SELECT scenario_id, status FROM scenarios'))
for line in (root / 'scenarios.jsonl').read_text().splitlines():
    scenario = json.loads(line)
    if len(scenario['ues']) == 1 and states.get(scenario['scenario_id'], 'pending') == 'pending':
        print(scenario['scenario_id'])
        break
PY
)"
echo "Selected scenario: $SCENARIO_ID"
```

If no ID is printed, stop and inspect checkpoint states; do not reset the
database. For a campaign without a single-UE set, select a pending scenario
from the intended set instead. Display the selected assignment before running:

```bash
python3 - "$AETHEREDGE_CAMPAIGN_DIR/scenarios.jsonl" "$SCENARIO_ID" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    scenario = next(json.loads(line) for line in stream
                    if json.loads(line)['scenario_id'] == sys.argv[2])
print(json.dumps(scenario, indent=2))
PY
```

For a physical-RF campaign, run without `--trace-bank`:

```bash
if [ -n "$SCENARIO_ID" ]; then
  ../gnb-core/env/bin/python tools/run_campaign.py \
    --campaign-dir "$AETHEREDGE_CAMPAIGN_DIR" \
    --scenario-id "$SCENARIO_ID"
fi
```

The runner prepares the assigned client, requests MEC release, waits for
transmission and drain, then validates the attempt. Other connected clients
remain idle except for control/association traffic. Do not press the Pixel
start button again. The runner can retry with new attempt IDs and invoke
recovery; allow it to finish. In a separate RAN Spark terminal, monitor the
campaign's `runner-events.jsonl` with `tail -f` if needed.

After the runner returns, inspect its checkpoint:

```bash
python3 - "$AETHEREDGE_CAMPAIGN_DIR/state.sqlite3" "$SCENARIO_ID" <<'PY'
import sqlite3
import sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True) as db:
    row = db.execute(
        'SELECT scenario_id, status, attempts, attempt_id, error FROM scenarios WHERE scenario_id = ?',
        (sys.argv[2],),
    ).fetchone()
print(row)
if row is None or row[1] != 'valid':
    raise SystemExit('Initial scenario is not valid; inspect runner events before continuing')
PY
```

Require `valid` before proceeding to the remaining campaign. The current
elapsed-time collection is not required to produce exactly 150 frames. Retain
the checkpoint and all failed-attempt evidence. A subsequent full campaign
invocation skips scenarios already marked valid; do not regenerate the
manifest or delete the checkpoint between the initial check and the full run.

## 21. Collect completed-campaign evidence onto RAN Spark

Wait for the runner to exit and finish recovery/cleanup. Do not start another
campaign, clear app data, regenerate the manifest, or delete failed attempts
before collecting evidence. Keep raw evidence separate from derived reports.
All commands below run on **RAN Spark**, including the SSH/SCP commands.

Set the campaign directory name, MEC `run_id`, and Orin log-directory name
explicitly. They can differ: the MEC run ID comes from the configuration used
for this run; the Orin directory comes from its supervisor's `--logs` argument.
Do not infer either from the manifest campaign ID when analyzing a copied repeat.

```bash
cd ~/mec-offload-ad/multi-ue
export CAMPAIGN=20261119-pilot-no-mps-no-tracking  # Replace for each campaign.
export RUN_ID=20261119-pilot-no-mps-no-tracking-collection
export ORIN_LOG_CAMPAIGN="$CAMPAIGN"  # Change if its supervisor used another path.
export MEC_HOST=sstreit@192.168.71.130
export ORIN_HOST=orin-ue
export REVIEW="$PWD/runs/campaigns/$CAMPAIGN/diagnostics"

mkdir -p "$REVIEW"/{ran,mec,orin,pixel,software}
cat "runs/campaigns/$CAMPAIGN/runner-status.json"
python3 - <<'PY'
import os
import sqlite3
from pathlib import Path
p = Path('runs/campaigns') / os.environ['CAMPAIGN'] / 'state.sqlite3'
with sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True) as db:
    print('Checkpoint counts:', db.execute(
        'SELECT status, COUNT(*) FROM scenarios GROUP BY status').fetchall())
PY
```

Check that the counts match the intended manifest. A completed runner is not,
by itself, proof that every scenario passed. Reports use the checkpoint's
accepted `attempt_id` for each `valid` scenario; retain other attempts for diagnosis.

### Current campaign files and event filtering

Use the manifest, lock, `state.sqlite3`, and runner logs directly from the parent
campaign directory; do not copy them into diagnostics. Do not copy RAN configs.

Create this helper once to retain only events with the exact MEC `RUN_ID`.
It preserves original event lines and all attempts, including failed attempts.
It leaves source files unchanged, prints progress every five seconds, and parses
only potentially matching records (rejecting malformed JSON in those records).
Each scan is bounded by the source file size at opening, so live telemetry
cannot extend it indefinitely. All historical files are still read from disk;
large archives take time, but unrelated records avoid expensive JSON parsing.
Empty input for
this run is an error unless `--allow-empty` is supplied for an unrelated
EdgeRIC instance.

```bash
cat > "$REVIEW/filter-run-events.py" <<'PY'
import argparse
import json
import os
import time
from pathlib import Path
from contextlib import ExitStack

parser = argparse.ArgumentParser()
parser.add_argument('source', type=Path)
parser.add_argument('destination', type=Path)
parser.add_argument('--allow-empty', action='store_true')
args = parser.parse_args()
run_id = os.environ['RUN_ID']
if not run_id:
    raise SystemExit('RUN_ID must not be empty')
needle = run_id.encode('utf-8')
if args.source.resolve() == args.destination.resolve():
    raise SystemExit('Source and destination must differ')
temp = args.destination.with_name(args.destination.name + '.partial')
count = scanned = number = 0
try:
    with args.source.open('rb') as source, ExitStack() as outputs:
        destination = None
        # Bound the scan so a live writer cannot keep it running indefinitely.
        size = os.fstat(source.fileno()).st_size
        print(f'Scanning {args.source} ({size / 1024**2:.1f} MiB)', flush=True)
        next_progress = time.monotonic() + 5
        while scanned < size:
            line = source.readline(size - scanned)
            if not line:
                raise SystemExit(f'Source was truncated during scan: {args.source}')
            scanned += len(line)
            number += 1
            if time.monotonic() >= next_progress:
                print(f'  {100 * scanned / size:.1f}% scanned; {count} matches', flush=True)
                next_progress = time.monotonic() + 5
            # Unrelated lines need no JSON parsing. Escaped JSON gets a fallback
            # parse so escaped run IDs cannot be incorrectly excluded.
            if needle not in line and b'\\' not in line:
                continue
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as error:
                raise SystemExit(f'{args.source}:{number}: {error}; recopy/retry if the writer was active')
            if event.get('run_id') == run_id:
                if destination is None:
                    args.destination.parent.mkdir(parents=True, exist_ok=True)
                    destination = outputs.enter_context(temp.open('wb'))
                destination.write(line.rstrip(b'\n') + b'\n')
                count += 1
    if count:
        temp.replace(args.destination)
    elif not args.allow_empty:
        raise SystemExit(f'No matching events in {args.source}; check RUN_ID')
    print(f'Done: {count} matching events from {args.source}', flush=True)
finally:
    temp.unlink(missing_ok=True)
PY
```

### EdgeRIC evidence

EdgeRIC obtains the MEC run ID during synchronization and includes it in event
records. Search every instance directory, since restarts produce new UUIDs and
one instance can span multiple campaigns. Save only matching event records:

```bash
for source in runs/edgeric/*/events.jsonl; do
  [ -f "$source" ] || continue
  instance="$(basename "$(dirname "$source")")"
  python3 "$REVIEW/filter-run-events.py" "$source" \
    "$REVIEW/ran/$instance/events.jsonl" --allow-empty || break
done
```

Instance directories are created only when at least one event matches `RUN_ID`.
Startup events with a null run ID
are excluded. Do not copy `associations.json`: it is an overwritten latest-state
snapshot that may describe a later run. Finalization association evidence is
already stored in the parent campaign's checkpoint. If parsing fails, inspect
or recopy the source before continuing; a live writer can leave an incomplete
last line. Do not treat a partial collection as complete.

### MEC evidence and configuration

Copy only `events.jsonl`, `jobs.jsonl`, and `run.json` from the MEC run directory.
Save these three evidence files in `diagnostics/mec/` (`$REVIEW/mec`).
Save the two YAML files directly in `diagnostics/` (`$REVIEW`).
Do not copy `replay.sqlite3` or its `-wal`/`-shm` files. Copy only the current
campaign YAML and `mec.collection.yaml`, both from **MEC Spark**. Ensure the
campaign YAML exists there and these configs have not been changed for a later
run. The parent manifest and lock remain authoritative for scenario assignments.

```bash
mkdir -p "$REVIEW/mec"
for name in events.jsonl jobs.jsonl run.json; do
  scp "${MEC_HOST}:~/mec-offload-ad/multi-ue/runs/${RUN_ID}/mec/${name}" \
    "$REVIEW/mec/$name" || break
done
scp "${MEC_HOST}:~/mec-offload-ad/multi-ue/config/campaigns/${CAMPAIGN}.yaml" \
  "$REVIEW/"
scp "${MEC_HOST}:~/mec-offload-ad/multi-ue/config/mec.collection.yaml" \
  "$REVIEW/"
ssh "$MEC_HOST" 'docker inspect --format "{{.Image}}" multi-ue-mec' \
  > "$REVIEW/software/mec-image-id.txt"
```

MEC events contain lifecycle, reception, resource, and error evidence; jobs
contain frame outcomes and processing timings. `run.json` is the latest runtime
snapshot. Use a unique MEC run ID per collection. The image ID identifies the
currently deployed image, so collect it before replacing the deployment.
Broad container logs and full configuration trees are not collected here.

### Pixel evidence

Connect each Pixel by USB. Pull only its event file into temporary storage and
filter it into diagnostics. Pulling the entire app directory can fail on
`receipts.sqlite3`; neither that database nor `run-as` is needed.

```bash
export PIXEL1=32110DLH20024R
export PIXEL2=2B251FDH200GK3
export PIXEL3=29171FDH2000UD
adb devices -l
```

Run this block once per connected phone, changing `ALIAS` and `SERIAL`:

```bash
ALIAS=pixel1
SERIAL="$PIXEL1"  # Use pixel2/$PIXEL2 and pixel3/$PIXEL3 for the other phones.
mkdir -p "$REVIEW/pixel/$ALIAS"
PIXEL_CAPTURE="$(mktemp -d)"
if adb -s "$SERIAL" pull \
    /sdcard/Android/data/edu.clemson.mecrtp/files/events.jsonl \
    "$PIXEL_CAPTURE/events.jsonl" && \
   python3 "$REVIEW/filter-run-events.py" "$PIXEL_CAPTURE/events.jsonl" \
    "$REVIEW/pixel/$ALIAS/events.jsonl"; then
  rm "$PIXEL_CAPTURE/events.jsonl"
  rmdir "$PIXEL_CAPTURE"
else
  echo "Collection failed; inspect temporary files in $PIXEL_CAPTURE"
fi
adb -s "$SERIAL" shell getprop ro.build.fingerprint \
  > "$REVIEW/pixel/$ALIAS/build-fingerprint.txt"
```

Do not copy unfiltered logcat history as current-run evidence. If troubleshooting
requires it, collect logcat separately with an explicit time range. Preserve all
matching event types here; analysis later selects accepted attempts and frames.

### Orin evidence

Check the saved launch record to confirm `ORIN_LOG_CAMPAIGN` matches the actual
`--logs` path. Collect its filtered event file and saved launch arguments only;
do not copy identity or receipt databases.

```bash
ssh "$ORIN_HOST" \
  'python3 -m json.tool ~/mec-offload-ad/multi-ue/runs/softue-client-launch.json'

mkdir -p "$REVIEW/orin/linux1"
ORIN_CAPTURE="$(mktemp -d)"
if scp "${ORIN_HOST}:~/mec-offload-ad/multi-ue/runs/${ORIN_LOG_CAMPAIGN}/ue/linux1/events.jsonl" \
    "$ORIN_CAPTURE/events.jsonl" && \
   python3 "$REVIEW/filter-run-events.py" "$ORIN_CAPTURE/events.jsonl" \
    "$REVIEW/orin/linux1/events.jsonl"; then
  rm "$ORIN_CAPTURE/events.jsonl"
  rmdir "$ORIN_CAPTURE"
else
  echo "Collection failed; inspect temporary files in $ORIN_CAPTURE"
fi
scp "${ORIN_HOST}:~/mec-offload-ad/multi-ue/runs/softue-client-launch.json" \
  "$REVIEW/orin/"
```

### Validate the copied event files

Check required files are nonempty and parse as JSON Lines. A parse failure may
indicate a failed copy or an incomplete final line from a live writer; recopy
and inspect rather than silently discarding evidence.

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['REVIEW'])
paths = [root / 'pixel' / alias / 'events.jsonl'
         for alias in ('pixel1', 'pixel2', 'pixel3')]
paths += [root / 'orin/linux1/events.jsonl']
paths += [root / 'mec' / name
          for name in ('events.jsonl', 'jobs.jsonl')]
edgeric = list((root / 'ran').glob('*/events.jsonl'))
if not edgeric:
    raise SystemExit('No matching EdgeRIC event files collected')
paths += edgeric
for path in paths:
    count = 0
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as error:
                raise SystemExit(f'{path}:{number}: {error}')
            if event.get('run_id') != os.environ['RUN_ID']:
                raise SystemExit(f'{path}:{number}: unexpected run_id')
            count += 1
    if not count:
        raise SystemExit(f'Empty file: {path}')
    print(count, path)
print('Required JSONL files are present and parseable.')
PY
```

Successful parsing is a transfer check, not a completeness audit. Analysis must
verify accepted-attempt coverage and join UE transmissions/receipts with MEC
jobs. Split single-UE and four-UE scenarios using the manifest. For the canonical
collection window, select frames whose first MEC packet arrival is in `[1 s,
6 s)` after the MEC scenario release, retaining later result receipts. Count
unknown window membership and missing receipts explicitly. The exporter’s
window rule is in [`export_campaign_dataset.py`](../tools/export_campaign_dataset.py).
Store generated summaries separately from this raw review directory.

## 22. Analyze and plot valid campaign results

After collecting Step 21 evidence, run these commands on **RAN Spark**. Both
commands take the campaign directory, not its `diagnostics` subdirectory:

```bash
cd ~/mec-offload-ad/multi-ue
CAMPAIGN=20261119-pilot-no-mps-no-tracking  # Replace for future campaigns.
python3 tools/campaign_analyze.py --campaign-dir "runs/campaigns/$CAMPAIGN"
python3 tools/campaign_plot.py --campaign-dir "runs/campaigns/$CAMPAIGN"
```

[`campaign_analyze.py`](../tools/campaign_analyze.py) uses Python's standard
library. [`campaign_plot.py`](../tools/campaign_plot.py) requires Matplotlib;
use a Python environment with Matplotlib installed. To create an isolated
plotting environment if your Python lacks it:

```bash
python3 -m venv /tmp/campaign-plots-venv
/tmp/campaign-plots-venv/bin/python -m pip install matplotlib
/tmp/campaign-plots-venv/bin/python tools/campaign_plot.py \
  --campaign-dir "runs/campaigns/$CAMPAIGN"
```

Outputs go to `runs/campaigns/$CAMPAIGN/diagnostics/results/`:

- `latency_summary.md`: UE and application tables plus pooled MEC stage timings.
- `summary.json`: all groups, methodology, evidence paths, coverage, and warnings.
- `<UE-group>-summary.json`: overall, per-UE, and per-application statistics,
  including mean, median, p95, p99, and sample counts for each latency metric.
- `frames.csv`: selected frame-level evidence used by the statistics and plots.
- `plots/<UE-group>/`: PNG and PDF empirical CDFs for overall UE latency,
  UE latency by application, UE latency by UE, and each MEC timing metric.
- `plots/index.json`: generated files and metrics skipped because no samples exist.

Groups use exact participating UE aliases, so different combinations with the
same UE count remain separate. Group membership is listed in the JSON and
Markdown. Statistics include only the checkpoint's selected valid attempts.
Frame selection uses MEC first-packet arrival in `[1 s, 6 s)` relative to MEC
release; result receipts after six seconds remain eligible. UE latency is
first result receipt minus transmission time on the same UE clock. Failed
result receipts are included in latency distributions. Missing receipts count
as SLO failures but cannot contribute a latency sample. The default SLO is
150 ms; `--slo-ms` on the analyzer explicitly overrides that threshold.

Read coverage warnings before interpreting results. Unknown arrival times are
excluded from window membership and counted separately. Missing stage timings
are not zero latency. MEC CUDA-event and inference timings overlap; do not add
them. A CDF's value at 150 ms is not necessarily the SLO success rate, since
failed results may arrive quickly. The analysis does not require raw EdgeRIC
logs for these latency metrics.

Rerunning analysis replaces its generated summaries and CSV; rerun plotting
afterward to update the CDFs. Raw evidence and the checkpoint are not modified.
