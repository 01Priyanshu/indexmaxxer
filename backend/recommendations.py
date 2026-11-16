import json
from pathlib import Path


def rank_results(report, min_gain_pct=5.0):
    """Rank individually tested indexes using measured read performance."""
    if not 0 <= min_gain_pct <= 100:
        raise ValueError("The minimum improvement must be between 0 and 100.")
    if report.get("status") != "complete" or not report.get("cleanup_verified"):
        raise ValueError("Use a completed comparison with verified cleanup.")

    rankings = []
    for result in report["results"]:
        candidate = result["candidate"]
        before = result["baseline"]["weighted_mean_of_medians_ms"]
        after = result["indexed"]["weighted_mean_of_medians_ms"]
        if before <= 0:
            raise ValueError("Baseline timing must be positive.")

        gain = 100 * (before - after) / before
        used_by = result["explain_queries_using_index"]

        reasons = []
        if not result.get("row_counts_match"):
            reasons.append("Result row-count check failed.")
        if not used_by:
            reasons.append("Index was not used in the diagnostic plans.")
        if gain < min_gain_pct:
            reasons.append(f"Improvement is below the {min_gain_pct:g}% threshold.")

        rankings.append({
            "index_name": candidate["index_name"],
            "schema": candidate["schema"],
            "table": candidate["table"],
            "columns": candidate["columns"],
            "baseline_ms": before,
            "indexed_ms": after,
            "improvement_pct": gain,
            "index_size_bytes": result["index_size_bytes"],
            "diagnostic_queries_using_index": used_by,
            "eligible": not reasons,
            "reason": " ".join(reasons) if reasons else (
                f"Measured {gain:.1f}% improvement and passed the read-performance checks."
            ),
        })

    rankings.sort(key=lambda r: (
        not r["eligible"], -r["improvement_pct"], r["index_size_bytes"],
        r["schema"], r["table"], tuple(r["columns"]),
    ))
    eligible = [r for r in rankings if r["eligible"]]

    return {
        "scope": "Individually tested indexes for the recorded read workload",
        "minimum_improvement_pct": min_gain_pct,
        "write_cost_evaluated": False,
        "selected_index": eligible[0] if eligible else None,
        "rankings": rankings,
    }


def load_latest_recommendations(results_dir, min_gain_pct=5.0):
    paths = sorted(Path(results_dir).glob("index_comparison_*.json"), reverse=True)

    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("status") == "complete" and report.get("cleanup_verified"):
            result = rank_results(report, min_gain_pct)
            result["source_report"] = path.name
            result["recorded_at_utc"] = report["recorded_at_utc"]
            result["dataset_estimated_rows"] = {
                table["table_name"]: table["estimated_rows"]
                for table in report["metadata_before"]["tables"]
            }
            return result

    raise FileNotFoundError("No completed index comparison was found.")
