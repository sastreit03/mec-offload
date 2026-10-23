#!/usr/bin/env python3
"""Join independently measured quality, processing, residence and encoded sizes.
No unmeasured candidate is admitted. Quality must use matched annotated data,
metric, precision and bitrate. Network initialization is a separate calibration.
"""

import argparse
import csv
import math
import statistics
from pathlib import Path
import yaml


def positive(x):
    x = float(x)
    if not math.isfinite(x) or x <= 0:
        raise ValueError("finite positive measurement required")
    return x


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", default="config/tasks.yaml")
    p.add_argument(
        "--measurements",
        required=True,
        help="CSV candidate_id,score,reference_score,processing_s,tracker_s,residence_s,frame_bytes_I,frame_bytes_P,metric,dataset,provenance",
    )
    p.add_argument("--network", required=True)
    p.add_argument("--compatibility", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    catalog = yaml.safe_load(Path(a.catalog).read_text())
    cs = {x["id"]: x for x in catalog["configurations"]}
    # Join measurements by catalog ID; the threshold is relative to the task reference.
    qualified = {}
    with open(a.measurements) as f:
        for r in csv.DictReader(f):
            cid = r["candidate_id"]
            c = cs[cid]
            metric = {
                "detection": "mAP50-95",
                "tracking": "HOTA",
                "segmentation": "mIoU",
            }[c["application"]]
            if r["metric"] != metric or not r["dataset"] or not r["provenance"]:
                raise ValueError("metric/dataset/provenance required")
            score, ref = positive(r["score"]), positive(r["reference_score"])
            if score < 0.9 * ref:
                continue
            qualified[cid] = dict(
                qualified=True,
                score=score,
                reference_score=ref,
                metric=metric,
                dataset=r["dataset"],
                provenance=r["provenance"],
                **{
                    k: positive(r[k])
                    for k in [
                        "processing_s",
                        "residence_s",
                        "frame_bytes_I",
                        "frame_bytes_P",
                    ]
                },
                tracker_s=positive(r["tracker_s"])
                if c["application"] == "tracking"
                else 0.0,
            )
    # Network calibration is supplied separately from model timing and accuracy.
    network = yaml.safe_load(Path(a.network).read_text())
    for key in [
        "mean_s",
        "P0_s2",
        "R_s2",
        "q_s2_per_s",
        "V_frame_s2",
        "radio_bps_per_unit",
    ]:
        positive(network[key])
    Path(a.output).write_text(
        yaml.safe_dump(
            dict(
                compatibility=a.compatibility,
                synthetic=False,
                network=network,
                candidates=qualified,
            ),
            sort_keys=False,
        )
    )
    print(
        f"{len(qualified)} candidates qualified; no timing, quality or network value was synthesized"
    )


if __name__ == "__main__":
    main()
