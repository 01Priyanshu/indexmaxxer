"""Measure per-query index benefit as the fraction of matching rows changes."""

import json
import platform
import statistics
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2 import sql

from backend.db import PROJECT_ROOT, get_connection
from backend.index_advisor import generate_candidates
from backend.metadata_analyzer import get_metadata
from backend.workload_analyzer import analyze_workload
from experiments.compare_indexes import index_layout
from experiments.compare_scales import CUSTOMERS, DATA_SEED, remove_owned_schema, seed_dataset
from experiments.compare_strategies import REPEATS, SEED, TRIALS, WARMUPS, evaluate_configuration

ORDER_ROWS = 100_000
TARGET_PERCENTAGES = (0.1, 1.0, 10.0, 50.0)


def query_specs():
    specs = []
    for number, target in enumerate(TARGET_PERCENTAGES, start=1):
        upper = round(CUSTOMERS * target / 100)
        if not 1 <= upper <= CUSTOMERS:
            raise ValueError("Invalid customer-ID range for selectivity experiment.")
        specs.append({"id": f"S{number}", "target_match_pct": target,
                      "customer_id_upper_bound": upper})
    return specs


def make_entries(conn, schema, specs):
    entries = []
    for spec in specs:
        statement = sql.SQL("SELECT * FROM {} WHERE customer_id <= {}").format(
            sql.Identifier(schema, "orders"),
            sql.Literal(spec["customer_id_upper_bound"]),
        )
        entries.append({"id": spec["id"], "frequency": 1,
                        "sql": statement.as_string(conn)})
    return entries


def scan_types(query):
    return list(dict.fromkeys(
        node["node_type"] for node in query["plan_summary"]["nodes"]
        if "Scan" in node["node_type"]
    ))


def summarize_selectivity(results, specs, candidate_name):
    if len(results) != TRIALS:
        raise RuntimeError("Incomplete selectivity trials.")
    expected_ids = {spec["id"] for spec in specs}
    paired_trials = []
    for trial in results:
        baseline = {q["id"]: q for q in trial["baseline"]["queries"]}
        indexed = {q["id"]: q for q in trial["indexed"]["queries"]}
        if (set(baseline) != expected_ids or set(indexed) != expected_ids
                or len(baseline) != len(trial["baseline"]["queries"])
                or len(indexed) != len(trial["indexed"]["queries"])):
            raise RuntimeError("Unexpected query IDs in a selectivity trial.")
        paired_trials.append((baseline, indexed))

    summary = []
    for spec in specs:
        before_values, after_values, gains, row_counts = [], [], [], []
        before_scans, after_scans, used = [], [], []
        for baseline, indexed in paired_trials:
            before_query, after_query = baseline[spec["id"]], indexed[spec["id"]]
            if before_query["sql"] != after_query["sql"]:
                raise RuntimeError("SQL changed between configurations.")
            if before_query["row_count"] != after_query["row_count"]:
                raise RuntimeError("Returned row counts changed between configurations.")
            before, after = before_query["median_ms"], after_query["median_ms"]
            if before <= 0 or after <= 0:
                raise RuntimeError("Invalid query timing.")
            before_values.append(before)
            after_values.append(after)
            gains.append(100 * (before - after) / before)
            row_counts.append(before_query["row_count"])
            before_scans.append(scan_types(before_query))
            after_scans.append(scan_types(after_query))
            used.append(any(node.get("index") == candidate_name
                            for node in after_query["plan_summary"]["nodes"]))
        if len(set(row_counts)) != 1 or not 0 <= row_counts[0] <= ORDER_ROWS:
            raise RuntimeError("Unexpected row-count variation between trials.")
        summary.append({
            **spec,
            "matching_rows": row_counts[0],
            "actual_match_pct": 100 * row_counts[0] / ORDER_ROWS,
            "baseline_ms_by_trial": before_values,
            "indexed_ms_by_trial": after_values,
            "paired_improvement_pct_by_trial": gains,
            "median_baseline_ms": statistics.median(before_values),
            "median_indexed_ms": statistics.median(after_values),
            "median_paired_improvement_pct": statistics.median(gains),
            "baseline_scan_types_by_trial": before_scans,
            "indexed_scan_types_by_trial": after_scans,
            "candidate_used_by_trial": used,
            "candidate_used_trials": sum(used),
            "trial_count": TRIALS,
        })
    return summary


def main():
    metadata = get_metadata()
    specs = query_specs()
    schema = "imx_selectivity_" + uuid4().hex
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    output = PROJECT_ROOT / "results" / f"selectivity_comparison_{stamp}.json"
    report = {
        "experiment": "customer_index_predicate_selectivity",
        "recorded_at_utc": now.isoformat(),
        "status": "incomplete", "cleanup_verified": False,
        "experiment_schema_removed": False,
        "source_metadata_before": metadata,
        "scratch_schema": schema,
        "orders_rows": ORDER_ROWS, "customer_rows": CUSTOMERS,
        "query_specs": specs,
        "client": {"python": platform.python_version(), "os": platform.platform()},
        "protocol": {
            "data_seed": DATA_SEED,
            "dataset": "Isolated synthetic orders/customers using seed_data.sql generation rules",
            "baseline": "Existing primary-key indexes only",
            "candidate": "Single-column B-tree on orders(customer_id)",
            "query_family": "SELECT * FROM orders WHERE customer_id <= upper_bound",
            "selectivity": "Actual returned rows divided by total orders; targets assume uniform customer IDs",
            "paired_trials": TRIALS,
            "warmup_rounds_per_phase": WARMUPS,
            "measured_rounds_per_phase": REPEATS,
            "phase_order": "Baseline first in trials 1 and 3; indexed first in trial 2",
            "measurement": "Per-query client execute + fetchall medians, in milliseconds",
            "summary": "Median across trials; gain is the median of paired query percentages",
            "diagnostics": "Index usage and scan types from separate EXPLAIN ANALYZE BUFFERS runs",
            "query_weights": "Equal experimental weights; pooled scores are not the original workload score",
            "cache_policy": "Warmups; no forced cache clearing",
            "optimizer": "Planner chooses freely; no scan method is forced",
            "trial_autovacuum": False,
            "statistics": "Explicit ANALYZE after data generation",
            "scope": "Read performance at four target match fractions; write costs excluded",
        },
        "results": [],
    }

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(output)

    save()
    print(f"Results file: {output}", flush=True)
    print(f"Preparing {ORDER_ROWS:,} orders and {CUSTOMERS:,} customers...", flush=True)
    try:
        with get_connection() as conn:
            created = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(510, 1)")
                    if not cur.fetchone()[0]:
                        raise RuntimeError("Another IndexMaxxer experiment is running.")
                    cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                    created = True
                seed_dataset(conn, schema, ORDER_ROWS)
                dataset_metadata = get_metadata(schema=schema)
                workload = analyze_workload(make_entries(conn, schema, specs), dataset_metadata)
                candidate = next((c for c in generate_candidates(workload, dataset_metadata)
                                  if c["table"] == "orders" and c["columns"] == ["customer_id"]), None)
                if candidate is None:
                    raise RuntimeError("The customer-ID index candidate was not generated.")
                report["dataset_metadata"] = dataset_metadata
                report["candidate"] = candidate
                report["workload_queries"] = [
                    {key: query[key] for key in ("id", "sql", "frequency")}
                    for query in workload["queries"]
                ]
                save()

                try:
                    with conn.cursor() as cur:
                        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE")
                    for trial in range(1, TRIALS + 1):
                        print(f"Trial {trial}/{TRIALS}: testing all four match fractions...", flush=True)
                        result = evaluate_configuration(
                            conn, workload, [candidate], trial % 2 == 1, SEED + trial,
                        )
                        result["trial"] = trial
                        report["results"].append(result)
                        save()
                        print(f"Trial {trial}/{TRIALS} complete; paired row counts match.", flush=True)
                finally:
                    conn.rollback()
                report["summary"] = summarize_selectivity(report["results"], specs, candidate["index_name"])
            finally:
                if created:
                    remove_owned_schema(conn, schema)
                    report["experiment_schema_removed"] = True
                else:
                    conn.rollback()
                save()

        report["source_metadata_after"] = get_metadata()
        report["source_index_layout_unchanged"] = (
            index_layout(metadata) == index_layout(report["source_metadata_after"])
        )
        report["cleanup_verified"] = (
            report["experiment_schema_removed"] and report["source_index_layout_unchanged"]
        )
        if not report["cleanup_verified"]:
            raise RuntimeError("Cleanup or original index-layout verification failed.")
        report["status"] = "complete"
        save()
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        save()
        raise

    print("\nSelectivity summary: per-query medians across 3 paired trials")
    for result in report["summary"]:
        plans = list(dict.fromkeys(" + ".join(types) for types in result["indexed_scan_types_by_trial"]))
        print(f"Target={result['target_match_pct']:g}% | Actual={result['actual_match_pct']:.3f}% | "
              f"Rows={result['matching_rows']:,}")
        print(f"  Baseline={result['median_baseline_ms']:.3f} ms | "
              f"Indexed={result['median_indexed_ms']:.3f} ms | "
              f"Paired gain={result['median_paired_improvement_pct']:+.1f}%")
        print(f"  Candidate in diagnostic plans: {result['candidate_used_trials']}/{TRIALS} | "
              f"Scans with index available: {'; '.join(plans)}")
    print("Small timing changes without index usage do not establish an index benefit.")
    print("Completed. Experiment schema removed; original index layout unchanged.")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
