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
