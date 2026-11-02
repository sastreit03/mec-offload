#!/usr/bin/env python3
"""Summarize requested, UE-encoded, and MEC-complete campaign throughput."""

import argparse
import csv
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path


def percentile(values, probability):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    position = (len(values) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def describe(values):
    values = [value for value in values if value is not None]
    if not values:
        return dict(mean=None, p05=None, p50=None, p95=None)
    return dict(
        mean=statistics.fmean(values),
        p05=percentile(values, 0.05),
        p50=percentile(values, 0.50),
        p95=percentile(values, 0.95),
    )


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def number(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def markdown_table(rows, columns):
    header = "| " + " | ".join(label for _, label in columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(str(row.get(key, "")) for key, _ in columns) + " |"
        for row in rows
    ]
    return "\n".join([header, divider, *body])


def load_streams(database):
    database.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in database.execute(
            """
            WITH transmission_totals AS (
              SELECT scenario_id, attempt_id, video_id,
                     COUNT(*) AS transmitted_frames,
                     SUM(encoded_bytes) AS ue_encoded_bytes,
                     MIN(tx_mono_ns) AS first_tx_mono_ns,
                     MAX(tx_mono_ns) AS last_tx_mono_ns
              FROM ue_transmissions
              GROUP BY scenario_id, attempt_id, video_id
            ), descriptor_totals AS (
              SELECT scenario_id, attempt_id, video_id,
                     COUNT(*) AS descriptor_frames,
                     SUM(encoded_bytes) AS descriptor_encoded_bytes,
                     MIN(packet_monotonic_ns) AS first_descriptor_mono_ns,
                     MAX(packet_monotonic_ns) AS last_descriptor_mono_ns
              FROM descriptors
              GROUP BY scenario_id, attempt_id, video_id
            ), complete_totals AS (
              SELECT scenario_id, attempt_id, video_id,
                     COUNT(*) AS mec_frame_records,
                     SUM(CASE
                           WHEN complete_reception_monotonic_ns IS NOT NULL
                           THEN encoded_bytes ELSE 0
                         END) AS mec_complete_bytes,
                     SUM(CASE
                           WHEN complete_reception_monotonic_ns IS NOT NULL
                           THEN 1 ELSE 0
                         END) AS mec_complete_frames
              FROM frames
              GROUP BY scenario_id, attempt_id, video_id
            )
            SELECT
              v.scenario_id, v.attempt_id, v.video_id, v.ue_alias, v.ue_kind,
              v.application, v.candidate_id, v.bitrate_bps AS requested_bps,
              v.frame_count AS expected_frames, s.duration_s, s.fps, s.ue_set,
              COALESCE(t.transmitted_frames, 0) AS transmitted_frames,
              t.ue_encoded_bytes,
              t.first_tx_mono_ns, t.last_tx_mono_ns,
              COALESCE(d.descriptor_frames, 0) AS descriptor_frames,
              d.descriptor_encoded_bytes,
              d.first_descriptor_mono_ns, d.last_descriptor_mono_ns,
              f.mec_frame_records,
              f.mec_complete_bytes,
              f.mec_complete_frames
            FROM videos AS v
            JOIN scenarios AS s ON s.scenario_id = v.scenario_id
            LEFT JOIN complete_totals AS f
              ON f.scenario_id = v.scenario_id
             AND f.attempt_id = v.attempt_id
             AND f.video_id = v.video_id
            LEFT JOIN transmission_totals AS t
              ON t.scenario_id = v.scenario_id
             AND t.attempt_id = v.attempt_id
             AND t.video_id = v.video_id
            LEFT JOIN descriptor_totals AS d
              ON d.scenario_id = v.scenario_id
             AND d.attempt_id = v.attempt_id
             AND d.video_id = v.video_id
            ORDER BY s.execution_index, v.ue_alias
            """
        )
    ]


def enrich(stream):
    duration = float(stream["duration_s"])
    requested = float(stream["requested_bps"])
    ue_encoded = stream["ue_encoded_bytes"]
    descriptor_encoded = stream["descriptor_encoded_bytes"]
    encoded = ue_encoded if ue_encoded is not None else descriptor_encoded
    stream["encoded_evidence_source"] = (
        "ue_transmission_log" if ue_encoded is not None
        else "mec_frame_descriptor" if descriptor_encoded is not None
        else None
    )
    stream["encoded_evidence_frames"] = (
        stream["transmitted_frames"]
        if ue_encoded is not None else stream["descriptor_frames"]
    )
    first_ns, last_ns = (
        (stream["first_tx_mono_ns"], stream["last_tx_mono_ns"])
        if ue_encoded is not None else
        (stream["first_descriptor_mono_ns"], stream["last_descriptor_mono_ns"])
    )
    stream["observed_span_s"] = (
        (last_ns - first_ns) / 1e9 + 1 / float(stream["fps"])
        if first_ns is not None and last_ns is not None else None
    )
    stream["requested_mbps"] = requested / 1e6
    stream["encoded_media_mbps"] = (
        encoded * 8 / duration / 1e6 if encoded is not None else None
    )
    stream["ue_achieved_mbps"] = (
        encoded * 8 / stream["observed_span_s"] / 1e6
        if encoded is not None and stream["observed_span_s"] else None
    )
    stream["mec_complete_mbps"] = (
        stream["mec_complete_bytes"] * 8 / duration / 1e6
        if stream["mec_frame_records"] is not None else None
    )
    stream["delta_mbps"] = (
        stream["ue_achieved_mbps"] - stream["requested_mbps"]
        if stream["ue_achieved_mbps"] is not None
        else None
    )
    stream["achieved_ratio"] = (
        stream["ue_achieved_mbps"] / stream["requested_mbps"]
        if stream["ue_achieved_mbps"] is not None
        else None
    )
    stream["mec_to_ue_ratio"] = (
        stream["mec_complete_mbps"] / stream["ue_achieved_mbps"]
        if stream["ue_achieved_mbps"] and stream["mec_complete_mbps"] is not None
        else None
    )
    stream["encoded_frame_coverage"] = (
        stream["encoded_evidence_frames"] / stream["expected_frames"]
    )
    stream["mec_complete_frame_coverage"] = (
        stream["mec_complete_frames"] / stream["expected_frames"]
        if stream["mec_complete_frames"] is not None else None
    )
    return stream


def bitrate_distribution(streams):
    groups = defaultdict(list)
    for stream in streams:
        groups[stream["requested_bps"]].append(stream)
    rows = []
    for requested, items in sorted(groups.items()):
        achieved = describe(item["ue_achieved_mbps"] for item in items)
        delta = describe(item["delta_mbps"] for item in items)
        ratio = describe(item["achieved_ratio"] for item in items)
        complete = describe(item["mec_complete_mbps"] for item in items)
        encoded_coverage = describe(
            item["encoded_frame_coverage"] for item in items
        )
        mec_coverage = describe(
            item["mec_complete_frame_coverage"] for item in items
        )
        available = [item for item in items if item["ue_achieved_mbps"] is not None]
        rows.append(
            dict(
                requested_mbps=requested / 1e6,
                streams=len(items),
                ue_evidence_streams=len(available),
                achieved_mean_mbps=achieved["mean"],
                achieved_p05_mbps=achieved["p05"],
                achieved_p50_mbps=achieved["p50"],
                achieved_p95_mbps=achieved["p95"],
                delta_mean_mbps=delta["mean"],
                ratio_p05=ratio["p05"],
                ratio_p50=ratio["p50"],
                ratio_p95=ratio["p95"],
                below_90_percent=100 * sum(
                    item["achieved_ratio"] < 0.9 for item in available
                ) / len(available) if available else None,
                mec_complete_mean_mbps=complete["mean"],
                encoded_frame_coverage_mean=encoded_coverage["mean"],
                mec_complete_frame_coverage_mean=mec_coverage["mean"],
            )
        )
    return rows


def device_bitrate_distribution(streams):
    rows = []
    kinds = sorted({stream["ue_kind"] for stream in streams})
    for kind in kinds:
        items = [stream for stream in streams if stream["ue_kind"] == kind]
        for row in bitrate_distribution(items):
            rows.append({"ue_kind": kind, **row})
    return rows


def scenario_distribution(streams):
    scenarios = defaultdict(list)
    for stream in streams:
        scenarios[(stream["scenario_id"], stream["ue_set"])].append(stream)
    totals = []
    for (scenario_id, ue_set), items in scenarios.items():
        descriptor_bytes = [item["descriptor_encoded_bytes"] for item in items]
        first = [
            item["first_descriptor_mono_ns"] for item in items
            if item["first_descriptor_mono_ns"] is not None
        ]
        last = [
            item["last_descriptor_mono_ns"] for item in items
            if item["last_descriptor_mono_ns"] is not None
        ]
        descriptor_complete = all(value is not None for value in descriptor_bytes)
        observed_span_s = (
            (max(last) - min(first)) / 1e9 + 1 / float(items[0]["fps"])
            if descriptor_complete and first and last else None
        )
        totals.append(
            dict(
                scenario_id=scenario_id,
                ue_set=ue_set,
                active_ues=len(items),
                requested_total_mbps=sum(item["requested_mbps"] for item in items),
                achieved_total_mbps=(
                    sum(descriptor_bytes) * 8 / observed_span_s / 1e6
                    if observed_span_s else None
                ),
                observed_span_s=observed_span_s,
                mec_complete_total_mbps=(
                    sum(item["mec_complete_mbps"] for item in items)
                    if all(item["mec_complete_mbps"] is not None for item in items)
                    else None
                ),
            )
        )
    for item in totals:
        item["delta_total_mbps"] = (
            item["achieved_total_mbps"] - item["requested_total_mbps"]
            if item["achieved_total_mbps"] is not None else None
        )
        item["total_ratio"] = (
            item["achieved_total_mbps"] / item["requested_total_mbps"]
            if item["achieved_total_mbps"] is not None else None
        )
    rows = []
    for ue_set in sorted({item["ue_set"] for item in totals}):
        items = [item for item in totals if item["ue_set"] == ue_set]
        requested = describe(item["requested_total_mbps"] for item in items)
        achieved = describe(item["achieved_total_mbps"] for item in items)
        delta = describe(item["delta_total_mbps"] for item in items)
        ratio = describe(item["total_ratio"] for item in items)
        complete = describe(item["mec_complete_total_mbps"] for item in items)
        rows.append(
            dict(
                ue_set=ue_set,
                scenarios=len(items),
                complete_ue_evidence=sum(
                    item["achieved_total_mbps"] is not None for item in items
                ),
                requested_mean_mbps=requested["mean"],
                achieved_mean_mbps=achieved["mean"],
                achieved_p05_mbps=achieved["p05"],
                achieved_p50_mbps=achieved["p50"],
                achieved_p95_mbps=achieved["p95"],
                delta_mean_mbps=delta["mean"],
                ratio_p50=ratio["p50"],
                mec_complete_mean_mbps=complete["mean"],
            )
        )
    return rows, totals


def requested_total_distribution(totals):
    groups = defaultdict(list)
    for item in totals:
        groups[item["requested_total_mbps"]].append(item)
    rows = []
    for requested, items in sorted(groups.items()):
        achieved = describe(item["achieved_total_mbps"] for item in items)
        delta = describe(item["delta_total_mbps"] for item in items)
        ratio = describe(item["total_ratio"] for item in items)
        complete = describe(item["mec_complete_total_mbps"] for item in items)
        rows.append(
            dict(
                requested_total_mbps=requested,
                scenarios=len(items),
                complete_ue_evidence=sum(
                    item["achieved_total_mbps"] is not None for item in items
                ),
                achieved_mean_mbps=achieved["mean"],
                achieved_p05_mbps=achieved["p05"],
                achieved_p50_mbps=achieved["p50"],
                achieved_p95_mbps=achieved["p95"],
                delta_mean_mbps=delta["mean"],
                ratio_p50=ratio["p50"],
                mec_complete_mean_mbps=complete["mean"],
            )
        )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    database = sqlite3.connect(args.database)
    streams = [enrich(item) for item in load_streams(database)]
    database.close()
    by_bitrate = bitrate_distribution(streams)
    by_device_bitrate = device_bitrate_distribution(streams)
    by_ue_set, scenario_totals = scenario_distribution(streams)
    by_requested_total = requested_total_distribution(scenario_totals)

    write_csv(args.output / "uplink_streams.csv", streams)
    write_csv(args.output / "uplink_by_bitrate.csv", by_bitrate)
    write_csv(args.output / "uplink_by_device_bitrate.csv", by_device_bitrate)
    write_csv(args.output / "uplink_scenario_totals.csv", scenario_totals)
    write_csv(args.output / "uplink_by_ue_set.csv", by_ue_set)
    write_csv(args.output / "uplink_by_requested_total.csv", by_requested_total)

    bitrate_display = [
        dict(
            requested=number(row["requested_mbps"]),
            n=f'{row["ue_evidence_streams"]}/{row["streams"]}',
            mean=number(row["achieved_mean_mbps"]),
            p05=number(row["achieved_p05_mbps"]),
            p50=number(row["achieved_p50_mbps"]),
            p95=number(row["achieved_p95_mbps"]),
            delta=number(row["delta_mean_mbps"]),
            ratio=number(row["ratio_p50"]),
            below=number(row["below_90_percent"], 1),
            mec=number(row["mec_complete_mean_mbps"]),
            encoded_coverage=number(
                100 * row["encoded_frame_coverage_mean"], 1
            ),
            mec_coverage=(
                number(100 * row["mec_complete_frame_coverage_mean"], 1)
                if row["mec_complete_frame_coverage_mean"] is not None else "n/a"
            ),
        )
        for row in by_bitrate
    ]
    total_display = [
        dict(
            ue_set=row["ue_set"],
            n=f'{row["complete_ue_evidence"]}/{row["scenarios"]}',
            requested=number(row["requested_mean_mbps"]),
            mean=number(row["achieved_mean_mbps"]),
            p05=number(row["achieved_p05_mbps"]),
            p50=number(row["achieved_p50_mbps"]),
            p95=number(row["achieved_p95_mbps"]),
            delta=number(row["delta_mean_mbps"]),
            ratio=number(row["ratio_p50"]),
            mec=number(row["mec_complete_mean_mbps"]),
        )
        for row in by_ue_set
    ]
    device_display = [
        dict(
            device=row["ue_kind"],
            requested=number(row["requested_mbps"]),
            n=f'{row["ue_evidence_streams"]}/{row["streams"]}',
            mean=number(row["achieved_mean_mbps"]),
            p05=number(row["achieved_p05_mbps"]),
            p50=number(row["achieved_p50_mbps"]),
            p95=number(row["achieved_p95_mbps"]),
            delta=number(row["delta_mean_mbps"]),
            ratio=number(row["ratio_p50"]),
        )
        for row in by_device_bitrate
    ]
    requested_total_display = [
        dict(
            requested=number(row["requested_total_mbps"]),
            n=f'{row["complete_ue_evidence"]}/{row["scenarios"]}',
            mean=number(row["achieved_mean_mbps"]),
            p05=number(row["achieved_p05_mbps"]),
            p50=number(row["achieved_p50_mbps"]),
            p95=number(row["achieved_p95_mbps"]),
            delta=number(row["delta_mean_mbps"]),
            ratio=number(row["ratio_p50"]),
            mec=number(row["mec_complete_mean_mbps"]),
        )
        for row in by_requested_total
    ]
    report = "\n".join(
        [
            "# Uplink throughput distribution",
            "",
            "Achieved throughput is encoded application payload divided by the "
            "observed first-to-last frame interval (including one frame period). "
            "UE transmission logs are used "
            "when present; otherwise the UE-declared encoded size in the MEC frame "
            "descriptor is used. MEC complete throughput includes only frames with "
            "complete reception evidence. Aggregate scenario throughput uses the "
            "common MEC descriptor-arrival interval. All values exclude protocol "
            "overhead.",
            "",
            "## Per-UE distribution by requested bitrate",
            "",
            markdown_table(
                bitrate_display,
                [
                    ("requested", "Requested Mbit/s"), ("n", "UE evidence/streams"),
                    ("mean", "Mean"), ("p05", "p05"), ("p50", "p50"),
                    ("p95", "p95"), ("delta", "Mean delta"),
                    ("ratio", "Median ratio"), ("below", "Below 90% (%)"),
                    ("mec", "MEC-complete mean"),
                    ("encoded_coverage", "Encoded frames (%)"),
                    ("mec_coverage", "MEC frames (%)"),
                ],
            ),
            "",
            "## Per-device distribution by requested bitrate",
            "",
            markdown_table(
                device_display,
                [
                    ("device", "Device"), ("requested", "Requested Mbit/s"),
                    ("n", "Evidence/streams"), ("mean", "Mean"),
                    ("p05", "p05"), ("p50", "p50"), ("p95", "p95"),
                    ("delta", "Mean delta"), ("ratio", "Median ratio"),
                ],
            ),
            "",
            "## Aggregate distribution by requested scenario throughput",
            "",
            markdown_table(
                requested_total_display,
                [
                    ("requested", "Requested total Mbit/s"),
                    ("n", "Complete evidence/scenarios"),
                    ("mean", "Mean"), ("p05", "p05"),
                    ("p50", "p50"), ("p95", "p95"),
                    ("delta", "Mean delta"), ("ratio", "Median ratio"),
                    ("mec", "MEC-complete mean"),
                ],
            ),
            "",
            "## Aggregate distribution by UE set",
            "",
            markdown_table(
                total_display,
                [
                    ("ue_set", "UE set"), ("n", "Complete evidence/scenarios"),
                    ("requested", "Requested mean Mbit/s"), ("mean", "Mean"),
                    ("p05", "p05"), ("p50", "p50"), ("p95", "p95"),
                    ("delta", "Mean delta"), ("ratio", "Median ratio"),
                    ("mec", "MEC-complete mean"),
                ],
            ),
            "",
        ]
    )
    (args.output / "uplink-throughput.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
