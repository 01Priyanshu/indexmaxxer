"""Export recorded experiment results as CSV tables and PNG/SVG figures."""

import csv
import hashlib
import json
import platform
import statistics
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backend.db import PROJECT_ROOT
from backend.write_results import load_recommendations_with_write_cost

BASE = "#8296ac"
INDEX = "#00877e"
OTHER = "#356ca5"
MUTED = "#717b88"


def latest(results_dir, pattern, matches=lambda report: True):
    for path in sorted(results_dir.glob(pattern), reverse=True):
        report = json.loads(path.read_text(encoding="utf-8"))
        if (report.get("status") == "complete"
                and report.get("cleanup_verified") is True and matches(report)):
            return path, report
    raise FileNotFoundError(f"No matching completed report: {pattern}")


def csv_table(folder, name, rows):
    with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def spread(values):
    median = statistics.median(values)
    return median, [[median - min(values)], [max(values) - median]]


def finish(fig, folder, name, title, note):
    fig.suptitle(title, fontsize=17, fontweight="bold", y=0.98)
    fig.text(0.025, 0.025, note, fontsize=8, color="#4b5b6b", va="bottom")
    fig.tight_layout(rect=(0, 0.14, 1, 0.9))
    for ax in fig.axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_axisbelow(True)
    fig.savefig(folder / f"{name}.png", dpi=220, bbox_inches="tight", facecolor="white")
    fig.savefig(folder / f"{name}.svg", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Exported {name}: CSV + PNG + SVG", flush=True)


def export_candidates(folder, recommendations):
    rows = [{"index": f"{r['table']}({', '.join(r['columns'])})",
             "baseline_ms": r["baseline_ms"], "indexed_ms": r["indexed_ms"],
             "measured_improvement_pct": r["improvement_pct"],
             "index_size_mib": r["index_size_bytes"] / 1048576,
             "shortlisted": r["eligible"]} for r in recommendations["rankings"]]
    csv_table(folder, "01_individual_indexes", rows)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    positions = list(range(len(rows)))
    ax.barh([y - .19 for y in positions], [r["baseline_ms"] for r in rows], .36,
            color=BASE, label="Paired baseline")
    ax.barh([y + .19 for y in positions], [r["indexed_ms"] for r in rows], .36,
            color=INDEX, label="With candidate")
    ax.set_yticks(positions, [r["index"].replace("(", "\n(", 1) for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("Frequency-weighted mean of query medians (ms)")
    ax.set_xlim(left=0)
    ax.grid(axis="x", alpha=.2)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.01), ncol=2, frameon=False)
    finish(fig, folder, "01_individual_indexes", "Individual index candidates",
           "Each candidate has its own paired baseline. Timings include client execute + fetchall.\n"
           "These are individually tested indexes; their benefits must not be added together.")


def export_writes(folder, write):
    rows = [{"operation": r["label"], "rows_per_batch": write["batch_rows"],
             "baseline_ms": r["baseline_median_ms"], "indexed_ms": r["indexed_median_ms"],
             "write_overhead_pct": r["overhead_pct"],
             "measured_repeats": write["measured_rounds"]} for r in write["operations"]]
    csv_table(folder, "02_write_costs", rows)
    fig, ax = plt.subplots(figsize=(11, 6))
    for y, operation in enumerate(write["operations"]):
        for offset, key, color, label in ((-.18, "baseline_samples_ms", BASE, "Baseline"),
                                         (.18, "indexed_samples_ms", INDEX, "With selected index")):
            median, errors = spread(operation[key])
            ax.barh(y + offset, median, .34, xerr=errors, capsize=3, color=color,
                    label=label if y == 0 else None)
    ax.set_yticks(range(len(rows)), [r["operation"] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel(f"Median time per {write['batch_rows']:,}-row batch (ms)")
    ax.set_xlim(left=0)
    ax.grid(axis="x", alpha=.2)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.01), ncol=2, frameon=False)
    finish(fig, folder, "02_write_costs", "Write cost of the selected index",
           f"Statement + commit timing. {write['measured_rounds']} measured batches per operation/configuration.\n"
           "Whiskers show observed minimum-to-maximum batch times, not confidence intervals.")


def export_strategies(folder, report):
    names = {"advisor_selected": "Selected\nsingle index", "naive_filtered_columns": "Naive\nfiltered-column indexes"}
    rows = [{"configuration": r["configuration"], "added_index_count": r["added_index_count"],
             "median_baseline_ms": r["median_baseline_ms"], "median_indexed_ms": r["median_indexed_ms"],
             "median_paired_improvement_pct": r["median_paired_improvement_pct"],
             "added_index_size_mib": r["median_added_index_bytes"] / 1048576}
            for r in report["summary"]]
    csv_table(folder, "03_strategy_comparison", rows)
    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    for x, result in enumerate(report["summary"]):
        median, errors = spread(result["indexed_ms_by_trial"])
        color = INDEX if result["configuration"] == "advisor_selected" else OTHER
        axes[0].bar(x, median, .55, yerr=errors, capsize=4, color=color)
        axes[1].bar(x, result["median_added_index_bytes"] / 1048576, .55, color=color)
    for ax in axes:
        ax.set_xticks(range(len(rows)), [names.get(r["configuration"], r["configuration"]) for r in rows])
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", alpha=.2)
    axes[0].set_ylabel("Weighted read time with configuration (ms)")
    axes[1].set_ylabel("Total additional index storage (MiB)")
    finish(fig, folder, "03_strategy_comparison", "Read speed and storage tradeoff",
           "Three paired trials per configuration. Read-time bars show medians; whiskers show trial min-max.\n"
           "The selected individual index is compared with a jointly tested index set on the same workload.")


def export_scales(folder, report):
    scales = sorted(report["scales"], key=lambda scale: scale["orders_rows"])
    if any(scale["status"] != "complete" or not scale["cleanup_verified"] for scale in scales):
        raise ValueError("Scale report contains incomplete datasets.")
    rows = [{"orders_rows": scale["orders_rows"], "customer_rows": scale["customer_rows"],
             "median_baseline_ms": scale["summary"]["median_baseline_ms"],
             "median_indexed_ms": scale["summary"]["median_indexed_ms"],
             "median_paired_improvement_pct": scale["summary"]["median_paired_improvement_pct"],
             "index_size_mib": scale["summary"]["median_added_index_bytes"] / 1048576}
            for scale in scales]
    csv_table(folder, "04_dataset_scale", rows)
    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    xs = [scale["orders_rows"] / 1000 for scale in scales]
    for key, color, label in (("baseline_ms_by_trial", BASE, "Baseline"),
                              ("indexed_ms_by_trial", INDEX, "With fixed index")):
        values = [spread(scale["summary"][key]) for scale in scales]
        axes[0].errorbar(xs, [v[0] for v in values],
                         yerr=[[v[1][0][0] for v in values], [v[1][1][0] for v in values]],
                         marker="o", capsize=4, color=color, label=label)
    axes[1].plot(xs, [row["index_size_mib"] for row in rows], marker="o", color=INDEX)
    for ax in axes:
        ax.set_xticks(xs)
        ax.set_xlabel("Orders (thousands)")
        ax.set_ylim(bottom=0)
        ax.grid(alpha=.2)
    axes[0].set_ylabel("Weighted read time (ms)")
    axes[0].legend()
    axes[1].set_ylabel("Additional index storage (MiB)")
    finish(fig, folder, "04_dataset_scale", "Selected index across dataset sizes",
           "10,000 customers; fixed index and query workload. Three paired trials per size.\n"
           "Read-time points show medians; whiskers show trial min-max. The index was not reselected at each size.")


def export_selectivity(folder, report):
    summary = sorted(report["summary"], key=lambda row: row["actual_match_pct"])
    rows = []
    for result in summary:
        used = result["candidate_used_trials"]
        scans = list(dict.fromkeys(" + ".join(types) for types in result["indexed_scan_types_by_trial"]))
        rows.append({"query": result["id"], "target_match_pct": result["target_match_pct"],
                     "actual_match_pct": result["actual_match_pct"], "matching_rows": result["matching_rows"],
                     "median_baseline_ms": result["median_baseline_ms"],
                     "median_index_available_ms": result["median_indexed_ms"],
                     "median_paired_timing_decrease_pct": result["median_paired_improvement_pct"],
                     "candidate_used_trials": used, "trial_count": result["trial_count"],
                     "scan_types_with_index_available": "; ".join(scans),
                     "interpretation": "Index unused; timing difference unexplained" if used == 0
                     else "Index observed in diagnostic plans; measured timing change"})
    csv_table(folder, "05_selectivity", rows)
    fig, axes = plt.subplots(1, 2, figsize=(12, 6.5))
    xs = [row["actual_match_pct"] for row in rows]
    for key, color, label in (("baseline_ms_by_trial", BASE, "Baseline"),
                              ("indexed_ms_by_trial", INDEX, "Index available")):
        values = [spread(result[key]) for result in summary]
        axes[0].errorbar(xs, [v[0] for v in values],
                         yerr=[[v[1][0][0] for v in values], [v[1][1][0] for v in values]],
                         marker="o", capsize=4, color=color, label=label)
    axes[0].set_xscale("log")
    axes[0].set_xticks(xs, [f"{x:.3f}%" for x in xs])
    axes[0].set_ylim(bottom=0)
    axes[0].set_xlabel("Actual matching rows (%) - log scale")
    axes[0].set_ylabel("Median query time (ms)")
    axes[0].legend()
    axes[0].grid(alpha=.2)
    for y, result in enumerate(summary):
        median, errors = spread(result["paired_improvement_pct_by_trial"])
        unused = result["candidate_used_trials"] == 0
        axes[1].errorbar(median, y, xerr=errors, fmt="o", capsize=4,
                         color=MUTED if unused else INDEX, mfc="white" if unused else INDEX,
                         markersize=7)
    labels = [f"{r['actual_match_pct']:.3f}%\n"
              + ("Index unused" if r["candidate_used_trials"] == 0
                 else f"Index used {r['candidate_used_trials']}/{r['trial_count']}") for r in rows]
    axes[1].set_yticks(range(len(rows)), labels)
    axes[1].set_ylim(len(rows) - .5, -.5)
    axes[1].axvline(0, color=MUTED, linewidth=.8)
    axes[1].set_xlabel("Measured paired timing decrease (%)")
    axes[1].grid(axis="x", alpha=.2)
    finish(fig, folder, "05_selectivity", "Selectivity, scan choice and measured timing",
           "Three paired trials; whiskers show observed trial min-max, not confidence intervals.\n"
           "The hollow point has no candidate-index use in its diagnostic plans: its timing change is unexplained.\n"
           "Client timings and EXPLAIN diagnostics are separate measurements; timing decreases are not automatically index effects.")


def main():
    results_dir = PROJECT_ROOT / "results"
    recommendations = load_recommendations_with_write_cost(results_dir)
    write = recommendations.get("write_evaluation")
    if not recommendations.get("write_cost_evaluated") or write is None:
        raise RuntimeError("A completed write report matching the selected read candidate is required.")
    read_source = recommendations["source_report"]
    strategy_path, strategy = latest(results_dir, "strategy_comparison_*.json",
                                     lambda r: r.get("source_read_report") == read_source)
    scale_path, scale = latest(results_dir, "scale_comparison_*.json",
                               lambda r: r.get("source_read_report") == read_source)
    selectivity_path, selectivity = latest(results_dir, "selectivity_comparison_*.json")
    sources = [results_dir / read_source, results_dir / write["source_report"],
               strategy_path, scale_path, selectivity_path]
    now = datetime.now(timezone.utc)
    folder = results_dir / ("exports_" + now.strftime("%Y%m%dT%H%M%S%fZ"))
    folder.mkdir()
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "svg.fonttype": "none", "axes.labelcolor": "#263e52"})
    export_candidates(folder, recommendations)
    export_writes(folder, write)
    export_strategies(folder, strategy)
    export_scales(folder, scale)
    export_selectivity(folder, selectivity)
    manifest = {"exported_at_utc": now.isoformat(), "python": platform.python_version(),
                "matplotlib": matplotlib.__version__,
                "sources": [{"filename": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                            for p in sources],
                "notes": ["Raw experiment reports were not modified.",
                          "Paired percentage medians need not equal percentages calculated from timing medians.",
                          "Whiskers are observed ranges, not confidence intervals or significance tests.",
                          "Selectivity timing changes without index use are marked as unexplained.",
                          "These figures cover synthetic experiments; they do not establish results on a real dataset."]}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nCompleted: 5 PNG graphs, 5 SVG graphs, 5 CSV tables, and manifest.json")
    print(f"Saved to: {folder}")


if __name__ == "__main__":
    main()
