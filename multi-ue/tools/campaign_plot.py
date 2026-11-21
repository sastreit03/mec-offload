#!/usr/bin/env python3
"""Plot empirical latency CDFs from campaign_analyze.py outputs.

Usage: python3 tools/campaign_plot.py --campaign-dir runs/campaigns/NAME
Requires matplotlib. Run campaign_analyze.py first; no source logs are rescanned.
"""
import argparse
import csv
import json
import math
from pathlib import Path

from campaign_analyze import STAGES


def ecdf(values):
    values = sorted(values)
    if not values:
        return [], []
    return [values[0], *values], [0.0, *[(i+1)/len(values) for i in range(len(values))]]


def plot_campaign(campaign_dir):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    results = Path(campaign_dir).resolve() / 'diagnostics/results'
    summary = json.loads((results/'summary.json').read_text())
    with (results/'frames.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for metric in ['ue_latency_ms', *STAGES]:
            raw = row.get(metric)
            value = float(raw) if raw else None
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f'Invalid {metric} in frames.csv')
            row[metric] = value
    generated = []; skipped = []
    for gid, group in summary['groups'].items():
        selected = [r for r in rows if r['ue_group'] == gid]
        if len(selected) != group['overall']['selected_frames']:
            raise ValueError(f'{gid}: CSV and summary counts differ; rerun analysis')
        directory = results/'plots'/gid
        directory.mkdir(parents=True, exist_ok=True)
        title_prefix = ', '.join(group['ue_aliases'])

        def draw(name, title, metric, series, slo=False):
            fig, ax = plt.subplots(figsize=(8, 5), layout='constrained')
            plotted = 0
            for label, subset in series:
                values = [r[metric] for r in subset if r[metric] is not None]
                if not values:
                    continue
                x, y = ecdf(values)
                ax.step(x, y, where='post', label=f'{label} (n={len(values)})', linewidth=1.6)
                plotted += 1
            if not plotted:
                plt.close(fig)
                skipped.append(dict(group=gid, plot=name, reason='No observed latency samples'))
                # Remove obsolete outputs from an earlier analysis of this group.
                for suffix in ('png','pdf'):
                    (directory/f'{name}.{suffix}').unlink(missing_ok=True)
                return
            if slo:
                ax.axvline(summary['slo_threshold_ms'], color='black', linestyle='--', alpha=.6,
                           label=f"SLO {summary['slo_threshold_ms']:g} ms")
            ax.set(title=f'{title}\n{title_prefix}', xlabel='Latency (ms)', ylabel='Empirical cumulative probability')
            ax.set_xlim(left=0); ax.set_ylim(0, 1.01)
            ax.grid(True, alpha=.25); ax.legend(fontsize='small')
            for suffix in ('png','pdf'):
                path=directory/f'{name}.{suffix}'
                fig.savefig(path, dpi=160)
                generated.append(str(path.relative_to(results)))
            plt.close(fig)

        draw('ue_overall_cdf', 'UE-observed end-to-end latency', 'ue_latency_ms', [('All UEs',selected)], True)
        draw('ue_per_application_cdf', 'UE latency by application', 'ue_latency_ms',
             [(app,[r for r in selected if r['application']==app]) for app in sorted(group['overall']['per_application'])], True)
        draw('ue_per_ue_cdf', 'UE latency by UE', 'ue_latency_ms',
             [(alias,[r for r in selected if r['ue_alias']==alias]) for alias in group['ue_aliases']], True)
        for stage in STAGES:
            draw(stage+'_cdf', stage.replace('_',' '), stage, [('All UEs',selected)])
    report = dict(campaign_id=summary['campaign_id'], run_id=summary['run_id'], generated=generated, skipped=skipped,
                  definition='ECDF of observed samples in the accepted-attempt MEC-arrival [1,6) window. Missing receipts/stages excluded. UE curves include failed result receipts; CDF at SLO is not necessarily SLO success rate.')
    (results/'plots/index.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f"Wrote {len(generated)} plot files to {results/'plots'}; skipped {len(skipped)} plots with no samples")
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign-dir',required=True,type=Path)
    args=parser.parse_args()
    plot_campaign(args.campaign_dir)


if __name__=='__main__':
    main()
