#!/usr/bin/env python3
"""Stage the assigned Linux-encoded H.264 segments on one USB Pixel at a time."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/sdcard/Movies/AetheredgeEncoded"
sys.path[:0] = [str(ROOT / "ue/linux/src"), str(ROOT / "contracts/python")]


def digest(path):
    with path.open("rb") as stream:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
        return checksum.hexdigest()


def asset_key(ue, fps, gop, width, height):
    sha = ue["media_sha256"].lower()
    if not re.fullmatch("[0-9a-f]{64}", sha):
        raise ValueError("invalid source SHA-256")
    # MEC sends stop at 7 seconds. Keep at least another second on the handset.
    count = max(int(ue["frame_count"]), 8 * fps)
    return (f"px1-{sha}-{ue['source_start_frame']}-{count}-{width}x{height}"
            f"-{fps}-{gop}-{ue['bitrate_bps']}"), count


def make_asset(output, key, source, start, count, width, height, fps, gop, bitrate):
    import cv2
    from mec_contracts.rtp import canonical_au
    from mec_ue.media import Encoder

    capture = cv2.VideoCapture(str(source))
    encoder = None
    try:
        if not capture.isOpened():
            raise ValueError(f"cannot open {source}")
        if start and (not capture.set(cv2.CAP_PROP_POS_FRAMES, start)
                      or round(capture.get(cv2.CAP_PROP_POS_FRAMES)) != start):
            raise ValueError(f"cannot seek to source frame {start}")
        encoder = Encoder(width, height, fps, bitrate)
        with output.open("wb") as stream:
            header = key.encode("ascii")
            stream.write(struct.pack(">4sH", b"PAV1", len(header)) + header)
            stream.write(struct.pack(">I", count))
            for i in range(count):
                ok, image = capture.read()
                if not ok:
                    raise ValueError(f"{source} ended before required frame {start + i}")
                force = i % gop == 0
                nals = encoder.frame(cv2.resize(image, (width, height)), i, fps, force, bitrate)
                types = {n[0] & 31 for n in nals}
                if force and not {5, 7, 8} <= types:
                    raise ValueError(f"frame {i} lacks required IDR/SPS/PPS")
                au = canonical_au(nals)
                stream.write(struct.pack(">IB", len(au), int(5 in types)))
                stream.write(au)
    finally:
        capture.release()
        if encoder is not None:
            encoder.close()


class Handset:
    def __init__(self, serial):
        self.command = ["adb", "-s", serial]

    def adb(self, *args):
        return subprocess.check_output(self.command + list(args), text=True).strip()

    def shell(self, *args):
        return self.adb("shell", shlex.join(args))

    def checksum(self, name):
        return self.shell("sha256sum", f"{REMOTE}/{name}").split()[0]

    def push(self, path):
        temp = f"{REMOTE}/.stage-{uuid.uuid4().hex}"
        try:
            self.adb("push", str(path), temp)
            if self.shell("sha256sum", temp).split()[0] != digest(path):
                raise ValueError(f"transfer verification failed for {path.name}")
            self.shell("mv", temp, f"{REMOTE}/{path.name}")
        finally:
            self.shell("rm", "-f", temp)


def matches(names, key):
    return sorted(n for n in names if re.fullmatch(re.escape(key) + r"-[0-9a-f]{64}\.pav", n))


def verified_name(name, actual):
    return name.endswith(f"-{actual}.pav")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--catalog", default=ROOT / "config/tasks.yaml", type=Path)
    parser.add_argument("--sources", type=Path, help="flat source inventory on Spark")
    parser.add_argument("--cache", type=Path, default=ROOT / "datasets/pixel-encoded")
    parser.add_argument("--ue", help="manifest UE alias for the connected Pixel")
    parser.add_argument("--serial", help="USB serial from adb devices")
    parser.add_argument("--check-staged", metavar="CAMPAIGN_YAML", type=Path,
                        help="check all participating Android aliases against saved USB receipts")
    args = parser.parse_args()
    manifest = args.campaign_dir / "scenarios.jsonl"
    lock = json.loads((args.campaign_dir / "manifest.lock.json").read_text())
    if digest(manifest) != lock["scenarios_sha256"] or digest(args.catalog) != lock["catalog_sha256"]:
        raise ValueError("manifest/catalog differs from campaign lock")
    height, width = yaml.safe_load(args.catalog.read_text())["source_hw"]
    receipts = args.campaign_dir / "pixel-staging"
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]

    def assignments(alias):
        jobs = {}
        for row in rows:
            fps, gop = row["fps"], row["gop_frames"]
            for ue in row["ues"]:
                if ue["ue_alias"] == alias:
                    key, count = asset_key(ue, fps, gop, width, height)
                    jobs[key] = ue, count, fps, gop
        return jobs

    if args.check_staged:
        if digest(args.check_staged) != lock["campaign_sha256"]:
            raise ValueError("campaign YAML differs from manifest lock")
        config = yaml.safe_load(args.check_staged.read_text())
        devices = set()
        for alias, settings in config["ues"].items():
            expected = assignments(alias)
            if settings["kind"] != "android" or not expected:
                continue
            receipt = json.loads((receipts / f"{alias}.json").read_text())
            if (receipt["manifest_sha256"] != lock["scenarios_sha256"]
                    or receipt["catalog_sha256"] != lock["catalog_sha256"]
                    or set(receipt["assets"]) != set(expected) or receipt["ue"] != alias
                    or receipt["serial"] in devices):
                raise ValueError(f"incomplete, stale or duplicate-device staging receipt: {alias}")
            devices.add(receipt["serial"])
            print(f"{alias}: {len(expected)} verified assets, USB {receipt['serial']}")
        return
    if not (args.serial and args.ue and args.sources):
        parser.error("staging requires --serial, --ue and --sources")
    jobs = assignments(args.ue)
    if not jobs:
        raise ValueError(f"no assignments for {args.ue}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.ue):
        raise ValueError("invalid UE alias")
    receipt_path = receipts / f"{args.ue}.json"
    receipt_path.unlink(missing_ok=True)
    phone = Handset(args.serial)
    phone.shell("mkdir", "-p", REMOTE)
    remote = phone.shell("ls", "-1", REMOTE).splitlines()
    args.cache.mkdir(parents=True, exist_ok=True)
    checked_sources = {}
    staged = {}
    for key, (ue, count, fps, gop) in jobs.items():
        candidates = matches(remote, key)
        valid = [n for n in candidates if verified_name(n, phone.checksum(n))]
        if len(valid) > 1:
            raise ValueError(f"ambiguous device encodes for {key}: {valid}")
        if valid:
            name = valid[0]
            print(f"device hit {name}", flush=True)
        else:
            path = next((p for p in sorted(args.cache.glob(key + "-*.pav"))
                         if verified_name(p.name, digest(p))), None)
            if path is None:
                source = args.sources / Path(ue["media_path"]).name
                if source not in checked_sources:
                    checked_sources[source] = digest(source)
                if checked_sources[source] != ue["media_sha256"].lower():
                    raise ValueError(f"source hash mismatch: {source}")
                with tempfile.TemporaryDirectory(dir=args.cache) as td:
                    temp = Path(td) / "encoded"
                    print(f"encoding {source.name} at {ue['bitrate_bps']} bps", flush=True)
                    make_asset(temp, key, source, ue["source_start_frame"], count, width, height,
                               fps, gop, ue["bitrate_bps"])
                    path = args.cache / f"{key}-{digest(temp)}.pav"
                    os.replace(temp, path)
            phone.push(path)
            name = path.name
            print(f"staged {name}", flush=True)
        # Retire only corrupt copies of this exact assignment after verified replacement.
        for stale in set(candidates) - {name}:
            phone.shell("mv", f"{REMOTE}/{stale}", f"{REMOTE}/{stale}.invalid")
        staged[key] = name
    receipts.mkdir(exist_ok=True)
    receipt = dict(ue=args.ue, serial=args.serial, manifest_sha256=lock["scenarios_sha256"],
                   catalog_sha256=lock["catalog_sha256"], assets=staged)
    with tempfile.NamedTemporaryFile(mode="w", dir=receipts, delete=False) as temp:
        json.dump(receipt, temp, indent=2)
    os.replace(temp.name, receipt_path)
    print(f"Verified {len(jobs)} distinct assets for {args.ue} on {args.serial} in {REMOTE}")


if __name__ == "__main__":
    main()
