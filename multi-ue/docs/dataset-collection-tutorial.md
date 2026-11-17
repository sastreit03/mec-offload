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
