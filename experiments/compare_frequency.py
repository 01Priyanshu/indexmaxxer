"""Offline frequency-weighting ablation using unchanged recorded query timings.

Run from the repository root: python -m experiments.compare_frequency
Optional: --source results/index_comparison_TIMESTAMP.json
No SQL is executed. The source report and dashboard recommendations are unchanged.
"""

import argparse
import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from backend.recommendations import rank_results


ROOT = Path(__file__).resolve().parents[1]


def positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric.")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive.")
    return value


def query_map(queries):
    mapped = {q["id"]: q for q in queries}
    if not mapped or len(mapped) != len(queries):
        raise ValueError("Queries must have unique IDs and must not be empty.")
    return mapped


def validate(report):
    if report.get("status") != "complete" or not report.get("cleanup_verified"):
        raise ValueError("A completed comparison with verified cleanup is required.")
    if not report.get("results"):
        raise ValueError("The source report has no tested candidates.")
    reference = query_map(report["results"][0]["baseline"]["queries"])
    weights = {qid: positive(q["frequency"], f"Frequency for {qid}")
               for qid, q in reference.items()}
    seen = set()
    for result in report["results"]:
        name = result["candidate"]["index_name"]
        if name in seen:
            raise ValueError("Duplicate candidate index name.")
        seen.add(name)
        before = query_map(result["baseline"]["queries"])
        after = query_map(result["indexed"]["queries"])
        if set(before) != set(reference) or set(after) != set(reference):
            raise ValueError("All candidates must use the same query IDs.")
        for qid, ref in reference.items():
            for q in (before[qid], after[qid]):
                if q["sql"] != ref["sql"] or q["frequency"] != weights[qid]:
                    raise ValueError(f"Workload changed between measurements: {qid}.")
                positive(q["median_ms"], f"Median time for {qid}")
            if before[qid]["row_count"] != after[qid]["row_count"]:
                raise ValueError(f"Paired row counts differ for {qid}.")
        if result.get("row_counts_match") is not True:
            raise ValueError("A source candidate failed its row-count check.")
        for phase in ("baseline", "indexed"):
            calculated = weighted_score(result[phase]["queries"], weights)
            saved = positive(result[phase]["weighted_mean_of_medians_ms"], "Saved score")
            if not math.isclose(calculated, saved, rel_tol=1e-6, abs_tol=1e-6):
                raise ValueError("Saved workload score does not match its query medians.")
    return weights


def weighted_score(queries, weights):
    return sum(weights[q["id"]] * q["median_ms"] for q in queries) / sum(weights.values())


def rescore(report, weights, min_gain_pct):
    changed = copy.deepcopy(report)
    for result in changed["results"]:
        for phase in ("baseline", "indexed"):
            measurement = result[phase]
            measurement["weighted_mean_of_medians_ms"] = weighted_score(
                measurement["queries"], weights
            )
            for query in measurement["queries"]:
                query["frequency"] = weights[query["id"]]
        before = result["baseline"]["weighted_mean_of_medians_ms"]
        after = result["indexed"]["weighted_mean_of_medians_ms"]
        result["improvement_pct"] = 100 * (before - after) / before
    ranked = rank_results(changed, min_gain_pct=min_gain_pct)
    ranked["scope"] = "Offline re-scoring of fixed candidates and recorded timings"
    return ranked


def label(row):
    if row is None:
        return "No eligible index"
    return f"{row['table']}({', '.join(row['columns'])})"


def analyze(report, min_gain_pct=5.0):
    weights = validate(report)
    aware = rescore(report, weights, min_gain_pct)
    equal = rescore(report, {qid: 1 for qid in weights}, min_gain_pct)
    common_rows = {row["index_name"]: row for row in aware["rankings"]}
    comparisons = []
    for name, ranked in (("frequency_aware", aware), ("equal_weights", equal)):
        selected = ranked["selected_index"]
        common = common_rows[selected["index_name"]] if selected else None
        comparisons.append({
            "selection_method": name,
            "selected_index": selected,
            "evaluation_with_recorded_frequencies": common,
        })
    names = [c["selected_index"]["index_name"] if c["selected_index"] else None
             for c in comparisons]
    return {
        "experiment": "offline_frequency_weighting_ablation",
        "status": "complete",
        "method": {
            "candidate_set": "All candidates in the source report; held fixed",
            "measurements": "Identical recorded per-query medians for both methods",
            "frequency_aware_weights": weights,
            "equal_weights": {qid: 1 for qid in weights},
            "ranking": "Existing backend.recommendations.rank_results",
            "minimum_improvement_pct": min_gain_pct,
            "common_evaluation": "Recorded query frequencies for both selections",
            "database_queries_executed": False,
            "limitations": [
                "This is in-sample re-scoring, not an independent runtime benchmark.",
                "Frequencies describe the synthetic workload, not production traffic.",
                "This isolates ranking weights, not candidate-generation ordering.",
                "Per-candidate paired baselines can differ because of timing variation.",
                "Equal weights apply to each query entry, not each normalized pattern.",
                "The 5% default threshold is a heuristic, not a significance test.",
            ],
        },
        "same_selection": names[0] == names[1],
        "both_methods_selected_an_index": all(name is not None for name in names),
        "methods": {"frequency_aware": aware, "equal_weights": equal},
        "selection_comparison": comparisons,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--minimum-gain", type=float, default=5.0)
    args = parser.parse_args()
    results_dir = ROOT / "results"
    source = args.source
    if source is None:
        for path in sorted(results_dir.glob("index_comparison_*.json"), reverse=True):
            candidate_report = json.loads(path.read_text(encoding="utf-8"))
            if candidate_report.get("status") == "complete" and candidate_report.get("cleanup_verified"):
                source = path
                break
    if source is None:
        raise FileNotFoundError("No completed individual-index comparison was found.")
    raw = source.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    output_report = analyze(report, args.minimum_gain)
    now = datetime.now(timezone.utc)
    output_report.update({
        "recorded_at_utc": now.isoformat(),
        "source_read_report": source.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_recorded_at_utc": report.get("recorded_at_utc"),
    })
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    output = results_dir / f"frequency_comparison_{stamp}.json"
    results_dir.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(output_report, handle, indent=2, allow_nan=False)

    print(f"Source: {source.name}")
    print("Offline comparison: same candidates and timings; only ranking weights change.")
    for name, ranked in output_report["methods"].items():
        print(f"\n{name}: selected {label(ranked['selected_index'])}")
        for number, row in enumerate(ranked["rankings"], 1):
            state = "eligible" if row["eligible"] else "filtered"
            print(f"  {number}. {label(row)} | gain={row['improvement_pct']:+.2f}% | {state}")
    print("\nBoth selections evaluated with the ORIGINAL query frequencies:")
    for comparison in output_report["selection_comparison"]:
        row = comparison["evaluation_with_recorded_frequencies"]
        if row:
            print(f"{comparison['selection_method']}: {label(row)} | "
                  f"baseline={row['baseline_ms']:.3f} ms | indexed={row['indexed_ms']:.3f} ms | "
                  f"gain={row['improvement_pct']:+.2f}%")
        else:
            print(f"{comparison['selection_method']}: no eligible index selected")
    if output_report["both_methods_selected_an_index"] and output_report["same_selection"]:
        print("\nBoth methods select the same index. This workload shows no selection benefit from weighting.")
    elif output_report["same_selection"]:
        print("\nNeither method selected an eligible index.")
    else:
        print("\nWeighting changes the selection in these saved measurements.")
    print("This is an in-sample analysis, not a new runtime benchmark.")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
