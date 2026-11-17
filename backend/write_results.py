"""Attach a matching, completed write experiment to read recommendations."""

import json
import math
import re
import statistics
from pathlib import Path

from backend.recommendations import load_latest_recommendations

OPERATIONS = (
    ("insert_batch", "INSERT orders"),
    ("update_customer_id", "UPDATE customer_id"),
    ("update_total_amount", "UPDATE total_amount"),
)


def candidate_key(candidate):
    if not isinstance(candidate, dict):
        return None
    columns = candidate.get("columns")
    fields = [candidate.get(key) for key in ("index_name", "schema", "table")]
    if not all(isinstance(value, str) and value for value in fields):
        return None
    if not isinstance(columns, list) or not columns:
        return None
    if not all(isinstance(column, str) and column for column in columns):
        return None
    return (*fields, tuple(columns))


def positive_integer(value, field):
    if type(value) is not int or value < 1:
        raise ValueError(f"Invalid write report field: {field}")
    return value


def summarize_write_report(report, filename):
    protocol = report["protocol"]
    repeats = positive_integer(protocol["measured_rounds"], "measured_rounds")
    batch_rows = positive_integer(report["batch_rows"], "batch_rows")
    source_rows = positive_integer(report["source_rows"], "source_rows")
    warmups = protocol["warmup_rounds"]
    if type(warmups) is not int or warmups < 0:
        raise ValueError("Invalid warmup count in write report.")
    expected_rounds = set(range(warmups + 1, warmups + repeats + 1))
    operations = []

    # Recompute from recorded samples; never use rounded console values.
    for operation, label in OPERATIONS:
        timings = {}
        for layout in ("baseline", "indexed"):
            samples = [sample for sample in report["samples"]
                       if sample["operation"] == operation
                       and sample["layout"] == layout
                       and sample["warmup"] is False]
            if len(samples) != repeats:
                raise ValueError(f"Incomplete write samples: {operation}/{layout}")
            if {sample["round"] for sample in samples} != expected_rounds:
                raise ValueError(f"Invalid measured rounds: {operation}/{layout}")
            if any(sample["rows_affected"] != batch_rows for sample in samples):
                raise ValueError("Write batch row counts do not match.")
            values = [sample["elapsed_ms"] for sample in samples]
            if any(type(value) not in (int, float)
                   or not math.isfinite(value) or value <= 0 for value in values):
                raise ValueError("Invalid write timing.")
            timings[layout] = values

        before = statistics.median(timings["baseline"])
        after = statistics.median(timings["indexed"])
        operations.append({
            "operation": operation,
            "label": label,
            "baseline_median_ms": before,
            "indexed_median_ms": after,
            "overhead_pct": 100 * (after / before - 1),
            "baseline_samples_ms": timings["baseline"],
            "indexed_samples_ms": timings["indexed"],
        })

    return {
        "source_report": filename,
        "source_read_report": report["source_read_report"],
        "recorded_at_utc": report["recorded_at_utc"],
        "candidate": {key: report["candidate"][key]
                      for key in ("index_name", "schema", "table", "columns")},
        "scope": "Write measurements for the selected read candidate only",
        "batch_rows": batch_rows,
        "source_rows": source_rows,
        "measured_rounds": repeats,
        "protocol": protocol,
        "operations": operations,
        "cleanup_verified": True,
        "note": "Positive overhead means slower writes. Read and write tests "
                "are separate; their percentages should not be subtracted.",
    }


def attach_write_results(recommendations, results_dir):
    result = dict(recommendations)
    result["write_cost_evaluated"] = False
    result["write_evaluation"] = None
    selected_key = candidate_key(result.get("selected_index"))
    if selected_key is None:
        return result

    paths = sorted(Path(results_dir).glob("write_comparison_*.json"), reverse=True)
    for path in paths:
        if not re.fullmatch(r"write_comparison_[0-9]{8}T[0-9]{12}Z\.json", path.name):
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError(f"Invalid write report: {path.name}")
        if report.get("status") != "complete" or report.get("cleanup_verified") is not True:
            continue
        if report.get("experiment") != "individual_index_write_comparison":
            continue
        if report.get("source_read_report") != result["source_report"]:
            continue
        if candidate_key(report.get("candidate")) != selected_key:
            continue
        result["write_evaluation"] = summarize_write_report(report, path.name)
        result["write_cost_evaluated"] = True
        break
    return result


def load_recommendations_with_write_cost(results_dir, min_gain_pct=5.0):
    recommendations = load_latest_recommendations(results_dir, min_gain_pct=min_gain_pct)
    return attach_write_results(recommendations, results_dir)
