"""Compare a previously selected index with all filtered-column indexes."""

import hashlib
import json
import platform
import random
import statistics
from datetime import datetime, timezone
from time import perf_counter_ns

from psycopg2 import sql

from backend.db import PROJECT_ROOT, get_connection
from backend.evaluation import benchmark_workload
from backend.index_advisor import generate_candidates
from backend.metadata_analyzer import get_metadata
from backend.recommendations import load_latest_recommendations
from backend.workload_analyzer import analyze_workload
from experiments.compare_indexes import index_layout

TRIALS = 3
WARMUPS = 2
REPEATS = 7
SEED = 42


def identity(candidate):
    return candidate["schema"], candidate["table"], tuple(candidate["columns"])


def query_signature(queries):
    signature = {q["id"]: (q["sql"], q["frequency"]) for q in queries}
    if len(signature) != len(queries):
        raise ValueError("Duplicate query IDs in the workload.")
    return signature


def naive_indexes(workload, metadata):
    """One single-column B-tree per WHERE column; skip existing equivalents."""
    filtered = sorted({
        (column["schema"], column["table"], column["column"])
        for query in workload["queries"]
        for column in query["features"]["where"]
    })
    tables = {(metadata["schema"], table["table_name"]): table
              for table in metadata["tables"]}
    candidates, skipped = [], []
    for schema, table, column in filtered:
        existing = [index["index_name"]
                    for index in tables[(schema, table)]["indexes"]
                    if index["method"] == "btree"
                    and index["is_valid"] and index["is_ready"]
                    and not index["is_partial"] and not index["has_expressions"]
                    and index["key_columns"] == [column]]
        if existing:
            skipped.append({"schema": schema, "table": table, "column": column,
                            "existing_indexes": existing})
            continue
        key = json.dumps([schema, table, column], separators=(",", ":"))
        name = "imx_naive_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        candidates.append({"index_name": name, "schema": schema,
                           "table": table, "columns": [column]})
    return candidates, skipped


def create_indexes(conn, candidates):
    created = []
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        cur.execute("SET LOCAL lock_timeout = '5s'")
        for candidate in candidates:
            statement = sql.SQL("CREATE INDEX {} ON {} USING btree ({})").format(
                sql.Identifier(candidate["index_name"]),
                sql.Identifier(candidate["schema"], candidate["table"]),
                sql.SQL(", ").join(sql.Identifier(c) for c in candidate["columns"]),
            )
            started = perf_counter_ns()
            cur.execute(statement)
            build_ms = (perf_counter_ns() - started) / 1_000_000
            cur.execute(
                "SELECT pg_relation_size(c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (candidate["schema"], candidate["index_name"]),
            )
            size = cur.fetchone()
            if size is None:
                raise RuntimeError("Could not measure a newly created index.")
            created.append({"candidate": candidate, "index_size_bytes": size[0],
                            "index_build_execute_ms": build_ms})
    return created


def evaluate_configuration(conn, workload, candidates, baseline_first, seed):
    def measure():
        return benchmark_workload(conn, workload, WARMUPS, REPEATS, seed)

    if baseline_first:
        baseline = measure()

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT imx_strategy_layout")
    try:
        created = create_indexes(conn, candidates)
        indexed = measure()
    finally:
        # Undo only this configuration's indexes before the next measurement.
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT imx_strategy_layout")
            cur.execute("RELEASE SAVEPOINT imx_strategy_layout")

    if not baseline_first:
        baseline = measure()

    before_counts = {q["id"]: q["row_count"] for q in baseline["queries"]}
    after_counts = {q["id"]: q["row_count"] for q in indexed["queries"]}
    if before_counts != after_counts:
        raise RuntimeError("Result row counts changed between configurations.")
    before = baseline["weighted_mean_of_medians_ms"]
    after = indexed["weighted_mean_of_medians_ms"]
    if before <= 0:
        raise RuntimeError("Baseline timing must be positive.")

    usage = {
        candidate["index_name"]: [
            q["id"] for q in indexed["queries"]
            if any(node.get("index") == candidate["index_name"]
                   for node in q["plan_summary"]["nodes"])
        ]
        for candidate in candidates
    }
    return {
        "phase_order": ["baseline", "indexed"] if baseline_first else ["indexed", "baseline"],
        "baseline": baseline,
        "indexed": indexed,
        "improvement_pct": 100 * (before - after) / before,
        "added_indexes": created,
        "total_added_index_bytes": sum(index["index_size_bytes"] for index in created),
        "queries_using_added_indexes": usage,
        "row_counts_match": True,
    }


def summarize(results, configurations):
    summaries = []
    for name, candidates in configurations.items():
        trials = [result for result in results if result["configuration"] == name]
        if len(trials) != TRIALS:
            raise RuntimeError("Incomplete configuration trials.")
        baseline = [trial["baseline"]["weighted_mean_of_medians_ms"] for trial in trials]
        indexed = [trial["indexed"]["weighted_mean_of_medians_ms"] for trial in trials]
        gains = [trial["improvement_pct"] for trial in trials]
        sizes = [trial["total_added_index_bytes"] for trial in trials]
        summaries.append({
            "configuration": name,
            "added_index_count": len(candidates),
            "baseline_ms_by_trial": baseline,
            "indexed_ms_by_trial": indexed,
            "improvement_pct_by_trial": gains,
            "added_index_bytes_by_trial": sizes,
            "median_baseline_ms": statistics.median(baseline),
            "median_indexed_ms": statistics.median(indexed),
            "median_paired_improvement_pct": statistics.median(gains),
            "median_added_index_bytes": statistics.median(sizes),
        })
    return summaries


def main():
    results_dir = PROJECT_ROOT / "results"
    metadata = get_metadata()
    entries = json.loads((PROJECT_ROOT / "data" / "workload.json").read_text(encoding="utf-8"))
    workload = analyze_workload(entries, metadata)
    recommendations = load_latest_recommendations(results_dir)
    selected = recommendations["selected_index"]
    if selected is None:
        raise RuntimeError("No selected read candidate is available.")

    candidates = generate_candidates(workload, metadata)
    advisor = next((c for c in candidates if identity(c) == identity(selected)), None)
    if advisor is None:
        raise RuntimeError("The previous recommendation is no longer a new candidate. "
                           "Check the workload and existing indexes.")

    original = json.loads((results_dir / recommendations["source_report"]).read_text(encoding="utf-8"))
    selected_run = next(result for result in original["results"]
                        if identity(result["candidate"]) == identity(selected))
    if query_signature(workload["queries"]) != query_signature(selected_run["baseline"]["queries"]):
        raise RuntimeError("The workload changed since index selection. "
                           "Run experiments.compare_indexes again before this comparison.")

    naive, skipped = naive_indexes(workload, metadata)
    if not naive:
        raise RuntimeError("All filtered columns already have equivalent indexes.")
    configurations = {"advisor_selected": [advisor], "naive_filtered_columns": naive}
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    output = results_dir / f"strategy_comparison_{stamp}.json"
    report = {
        "experiment": "selected_index_vs_naive_configuration",
        "recorded_at_utc": now.isoformat(),
        "source_read_report": recommendations["source_report"],
        "selection_dataset_estimated_rows": recommendations["dataset_estimated_rows"],
        "status": "incomplete", "cleanup_verified": False,
        "client": {"python": platform.python_version(), "os": platform.platform()},
        "metadata_before": metadata,
        "configurations": configurations,
        "naive_existing_indexes_skipped": skipped,
        "protocol": {
            "paired_trials_per_configuration": TRIALS,
            "warmup_rounds_per_phase": WARMUPS,
            "measured_rounds_per_phase": REPEATS,
            "shuffle_seed": SEED,
            "baseline": "All existing indexes; no experiment indexes",
            "measurement": "Frequency-weighted mean of per-query client execute + fetchall medians",
            "phase_order": "Baseline first in trials 1 and 3; indexed first in trial 2",
            "configuration_order": "Shuffled within each trial",
            "summary": "Median across trials; improvement is the median of paired percentages",
            "cache_policy": "Warmups; no forced cache clearing",
            "isolation": "One REPEATABLE READ transaction for all measured configurations",
            "cleanup": "Savepoint rollback per configuration and full rollback at the end",
            "scope": "Read performance and added index storage; write costs are not compared here",
            "selection": "Candidate selected on this same workload in a previous experiment; not held-out validation",
        },
        "results": [],
    }

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(output)

    save()
    print(f"Results file: {output}", flush=True)
    for name, indexes in configurations.items():
        labels = [f"{c['table']}({', '.join(c['columns'])})" for c in indexes]
        print(f"{name}: {' + '.join(labels)}", flush=True)

    try:
        rng = random.Random(SEED)
        with get_connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE")
                    cur.execute("SELECT pg_try_advisory_xact_lock(510, 1)")
                    if not cur.fetchone()[0]:
                        raise RuntimeError("Another IndexMaxxer experiment is running.")
                for trial in range(1, TRIALS + 1):
                    order = list(configurations)
                    rng.shuffle(order)
                    for name in order:
                        print(f"\nTrial {trial}/{TRIALS}: {name}", flush=True)
                        result = evaluate_configuration(
                            conn, workload, configurations[name], trial % 2 == 1, SEED + trial,
                        )
                        result.update({"trial": trial, "configuration": name})
                        report["results"].append(result)
                        save()
                        before = result["baseline"]["weighted_mean_of_medians_ms"]
                        after = result["indexed"]["weighted_mean_of_medians_ms"]
                        size = result["total_added_index_bytes"] / (1024 * 1024)
                        print(f"Baseline={before:.3f} ms | Indexed={after:.3f} ms | "
                              f"Gain={result['improvement_pct']:+.1f}% | Added indexes={size:.2f} MiB",
                              flush=True)
            finally:
                conn.rollback()

        report["metadata_after"] = get_metadata()
        report["cleanup_verified"] = index_layout(metadata) == index_layout(report["metadata_after"])
        if not report["cleanup_verified"]:
            raise RuntimeError("The original index layout was not restored; inspect the report.")
        report["summary"] = summarize(report["results"], configurations)
        report["status"] = "complete"
        save()
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            report["metadata_after"] = get_metadata()
            report["cleanup_verified"] = index_layout(metadata) == index_layout(report["metadata_after"])
        except Exception as cleanup_error:
            report["cleanup_check_error"] = str(cleanup_error)
        save()
        raise

    print("\nSummary: medians across 3 paired trials")
    for result in report["summary"]:
        print(f"{result['configuration']}: "
              f"baseline={result['median_baseline_ms']:.3f} ms | "
              f"indexed={result['median_indexed_ms']:.3f} ms | "
              f"paired gain={result['median_paired_improvement_pct']:+.1f}% | "
              f"added size={result['median_added_index_bytes'] / (1024 * 1024):.2f} MiB")
    print("Completed. Original index layout restored.")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
