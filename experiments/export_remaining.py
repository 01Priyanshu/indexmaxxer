"""Export completed frequency, skew and selected TPC-H results; no DB required."""
import csv
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
BASE, INDEX = '#8296ac', '#00877e'


def latest(pattern, cleanup=True):
    for path in sorted((ROOT / 'results').glob(pattern), reverse=True):
        raw = path.read_bytes()
        report = json.loads(raw.decode('utf-8'))
        if report.get('status') == 'complete' and (not cleanup or report.get('cleanup_verified')):
            return path, report, hashlib.sha256(raw).hexdigest()
    raise FileNotFoundError(f'No completed report found: {pattern}')


def table(folder, name, rows):
    with (folder / f'{name}.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def finish(fig, folder, name, note):
    fig.text(.025, .02, note, fontsize=8, va='bottom', color='#435366')
    fig.tight_layout(rect=(0, .15, 1, .95))
    for ax in fig.axes:
        ax.spines[['top', 'right']].set_visible(False)
        ax.grid(axis='x', alpha=.18)
        ax.set_axisbelow(True)
    for extension in ('png', 'svg'):
        fig.savefig(folder / f'{name}.{extension}', dpi=220, facecolor='white', bbox_inches='tight')
    plt.close(fig)
    print(f'Exported {name}: PNG + SVG + CSV', flush=True)


def bar(ax, y, values, color, label):
    median = statistics.median(values)
    ax.barh(y, median, .32, color=color, label=label,
            xerr=[[median-min(values)], [max(values)-median]], capsize=3)


def export_frequency(folder, report):
    methods = report['methods']
    aware = methods['frequency_aware']['rankings']
    equal = {r['index_name']: r for r in methods['equal_weights']['rankings']}
    rows = [{'index': f"{r['table']}({', '.join(r['columns'])})",
             'recorded_frequency_gain_pct': r['improvement_pct'],
             'equal_weight_gain_pct': equal[r['index_name']]['improvement_pct'],
             'recorded_frequency_eligible': r['eligible'],
             'equal_weight_eligible': equal[r['index_name']]['eligible']} for r in aware]
    table(folder, '06_frequency_weights', rows)
    selections = []
    for c in report['selection_comparison']:
        r = c['evaluation_with_recorded_frequencies']
        selections.append({'method': c['selection_method'],
            'selected_index': f"{r['table']}({', '.join(r['columns'])})" if r else '',
            'common_weight_baseline_ms': r['baseline_ms'] if r else '',
            'common_weight_indexed_ms': r['indexed_ms'] if r else '',
            'common_weight_gain_pct': r['improvement_pct'] if r else ''})
    table(folder, '06_frequency_selections', selections)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    for y, r in enumerate(rows):
        ax.barh(y-.18, r['recorded_frequency_gain_pct'], .32, color=INDEX,
                label='Recorded frequency weights' if y == 0 else None)
        ax.barh(y+.18, r['equal_weight_gain_pct'], .32, color=BASE,
                label='Equal query weights' if y == 0 else None)
    ax.set_yticks(range(len(rows)), [r['index'] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel('Read-improvement score under each weighting scheme (%)')
    ax.set_title('Effect of query-frequency weighting')
    ax.legend(loc='lower right')
    status = 'Both methods selected the same index.' if report['same_selection'] else 'The selection differed between weighting schemes.'
    finish(fig, folder, '06_frequency_weights',
           'Offline re-scoring of identical timings and fixed candidates; no new runtime measurements.\n'
           'The two scores use different query weights; their difference is not an extra speedup.\n' + status)


def export_skew(folder, report):
    rows, query_rows = [], []
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for y, item in enumerate(report['distributions']):
        s = item['summary']
        rows.append({'distribution': item['distribution'],
                     'hot_customer_share_pct': item['data_profile']['actual_hot_customer_share_pct'],
                     'median_baseline_ms': s['median_baseline_ms'],
                     'median_indexed_ms': s['median_indexed_ms'],
                     'median_paired_gain_pct': s['median_paired_improvement_pct'],
                     'index_size_mib': s['median_added_index_bytes']/1048576})
        bar(ax, y-.18, s['baseline_ms_by_trial'], BASE, 'Paired baseline' if y == 0 else None)
        bar(ax, y+.18, s['indexed_ms_by_trial'], INDEX, 'With customer_id index' if y == 0 else None)
        for q in item['query_summary']:
            query_rows.append({'distribution': item['distribution'], 'query': q['id'],
                'rows_returned': q['row_counts_by_trial'][0],
                'baseline_ms': q['median_baseline_ms'], 'indexed_ms': q['median_indexed_ms'],
                'paired_gain_pct': q['median_paired_improvement_pct'],
                'trials_using_index': q['diagnostic_trials_using_index']})
    table(folder, '07_data_skew', rows)
    table(folder, '07_data_skew_queries', query_rows)
    ax.set_yticks(range(len(rows)), [r['distribution'].capitalize() for r in rows])
    ax.invert_yaxis()
    ax.set_xlim(left=0)
    ax.set_xlabel('Frequency-weighted mean of query medians (ms)')
    ax.set_title('Uniform versus skewed customer distribution')
    ax.legend()
    finish(fig, folder, '07_data_skew',
           '100K orders, 10K customers; fixed customer_id index. Whiskers: observed trial range, not confidence intervals.\n'
           'Skew puts about 80% of orders among 100 customers. Same query literals can return more rows.\n'
           'Non-customer columns are identical. Timing changes without index use do not establish index benefit.')


def export_tpch(folder, report):
    rows, totals = [], []
    summaries = report['summary']
    fig, axes = plt.subplots(1, len(summaries), figsize=(12, 6), squeeze=False)
    for ax, s in zip(axes[0], summaries):
        totals.append({'configuration': s['configuration'],
                       'median_baseline_ms': s['median_baseline_ms'],
                       'median_indexed_ms': s['median_indexed_ms'],
                       'median_paired_gain_pct': s['median_paired_improvement_pct'],
                       'added_index_size_mib': s['median_added_index_bytes']/1048576})
        for y, q in enumerate(s['queries']):
            bar(ax, y-.18, q['baseline_ms_by_trial'], BASE, 'Paired baseline' if y == 0 else None)
            bar(ax, y+.18, q['indexed_ms_by_trial'], INDEX, 'With layout' if y == 0 else None)
            rows.append({'configuration': s['configuration'], 'query': q['id'],
                         'baseline_ms': q['median_baseline_ms'], 'indexed_ms': q['median_indexed_ms'],
                         'paired_gain_pct': q['median_paired_improvement_pct'],
                         'trials_using_added_index': q['diagnostic_trials_using_added_index']})
        ax.set_yticks(range(len(s['queries'])), [f"{q['id']}\nindex used {q['diagnostic_trials_using_added_index']}/3" for q in s['queries']])
        ax.invert_yaxis()
        ax.set_xlim(left=0)
        ax.set_xlabel('Client execute + fetchall median (ms)')
        ax.set_title(s['configuration'].replace('_', ' ').capitalize())
        ax.legend(fontsize=8)
    fig.suptitle(f"Selected TPC-H-derived queries on PostgreSQL (SF {report['scale_factor']})", fontweight='bold')
    table(folder, '08_selected_tpch', rows)
    table(folder, '08_selected_tpch_layouts', totals)
    finish(fig, folder, '08_selected_tpch',
           'DuckDB-generated data/query text; PostgreSQL runs all timed queries. Hand-defined layouts, not advisor selections.\n'
           'Three paired trials; whiskers show observed ranges. Index use comes from separate diagnostic plans.\n'
           'A selected-query experiment, not an official TPC-H benchmark. No QphH or significance claim.')


def main():
    inputs = [latest('frequency_comparison_*.json', cleanup=False),
              latest('skew_comparison_*.json'), latest('tpch_comparison_*.json')]
    if inputs[0][1]['source_read_report'] != inputs[1][1]['source_read_report']:
        raise ValueError('Frequency and skew reports refer to different original index selections.')
    now = datetime.now(timezone.utc)
    folder = ROOT / 'results' / f"additional_exports_{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    folder.mkdir(parents=True)
    export_frequency(folder, inputs[0][1])
    export_skew(folder, inputs[1][1])
    export_tpch(folder, inputs[2][1])
    manifest = {'recorded_at_utc': now.isoformat(),
                'sources': [{'file': p.name, 'sha256': digest} for p, r, digest in inputs],
                'matplotlib_version': matplotlib.__version__,
                'files': sorted(p.name for p in folder.iterdir())}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'\nSaved 3 PNGs, 3 SVGs, 6 CSVs and manifest.json to: {folder}')


if __name__ == '__main__':
    main()
