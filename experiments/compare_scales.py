"""Measure a fixed selected index across isolated synthetic dataset sizes."""

import json
import platform
from datetime import datetime, timezone
from uuid import uuid4

import sqlglot
from psycopg2 import sql
from sqlglot import exp

from backend.db import PROJECT_ROOT, get_connection
from backend.index_advisor import generate_candidates
from backend.metadata_analyzer import get_metadata
from backend.recommendations import load_latest_recommendations
from backend.workload_analyzer import analyze_workload
from experiments.compare_indexes import index_layout
from experiments.compare_strategies import (
    REPEATS, SEED, TRIALS, WARMUPS,
    evaluate_configuration, identity, query_signature, summarize,
)

SCALES = (100_000, 500_000, 1_000_000)
CUSTOMERS = 10_000
DATA_SEED = 0.42
SOURCE_SCHEMA = "benchmark"


def retarget_entries(queries, schema):
    """Change table schemas through the SQL tree, preserving query literals."""
    entries = []
    for query in queries:
        tree = sqlglot.parse_one(query["sql"], read="postgres")
        tables = list(tree.find_all(exp.Table))
        if not tables:
            raise ValueError("A scale query must reference a benchmark table.")
        for table in tables:
            if table.catalog or table.db != SOURCE_SCHEMA:
                raise ValueError("Scale workloads must use explicit benchmark tables.")
            if table.name not in {"customers", "orders"}:
                raise ValueError("Unsupported table in scale workload.")
            table.set("db", exp.to_identifier(schema, quoted=True))
        # Qualified columns are uncommon after analysis, but handle them too.
        for column in tree.find_all(exp.Column):
            if column.db:
                if column.catalog or column.db != SOURCE_SCHEMA:
                    raise ValueError("Unsupported qualified column in scale workload.")
                column.set("db", exp.to_identifier(schema, quoted=True))
        entries.append({"id": query["id"], "frequency": query["frequency"],
                        "sql": tree.sql(dialect="postgres")})
    return entries


def seed_dataset(conn, schema, order_rows):
    customers = sql.Identifier(schema, "customers")
    orders = sql.Identifier(schema, "orders")
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '180s'")
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute(sql.SQL("""
            CREATE TABLE {} (
                customer_id INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL,
                region TEXT NOT NULL
            ) WITH (fillfactor=100, autovacuum_enabled=false)
        """).format(customers))
        cur.execute(sql.SQL("""
            CREATE TABLE {} (
                order_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES {}(customer_id),
                order_date DATE NOT NULL,
                status TEXT NOT NULL,
                total_amount NUMERIC(10,2) NOT NULL
            ) WITH (fillfactor=100, autovacuum_enabled=false)
        """).format(orders, customers))
        cur.execute("SELECT setseed(%s)", (DATA_SEED,))
        cur.execute(sql.SQL("""
            INSERT INTO {} (customer_id, customer_name, region)
            SELECT id, 'Customer ' || id,
                   (ARRAY['North', 'South', 'East', 'West'])[random(1, 4)]
            FROM generate_series(1, %s) AS g(id)
        """).format(customers), (CUSTOMERS,))
        if cur.rowcount != CUSTOMERS:
            raise RuntimeError("Unexpected number of generated customers.")
        cur.execute(sql.SQL("""
            INSERT INTO {} (customer_id, order_date, status, total_amount)
            SELECT random(1, %s),
                   DATE '2025-01-01' + random(0, 729),
                   (ARRAY['pending', 'shipped', 'delivered', 'cancelled'])[random(1, 4)],
                   round((10 + random() * 990)::numeric, 2)
            FROM generate_series(1, %s)
        """).format(orders), (CUSTOMERS, order_rows))
        if cur.rowcount != order_rows:
            raise RuntimeError("Unexpected number of generated orders.")
        cur.execute(sql.SQL("ANALYZE {}").format(customers))
        cur.execute(sql.SQL("ANALYZE {}").format(orders))
        cur.execute(sql.SQL("""
            SELECT (SELECT count(*) FROM {}), (SELECT count(*) FROM {})
        """).format(customers, orders))
        counts = cur.fetchone()
        if counts != (CUSTOMERS, order_rows):
            raise RuntimeError("Generated table row counts do not match the requested size.")
    conn.commit()


def remove_owned_schema(conn, schema):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (schema,))
        removed = cur.fetchone()[0] == 0
    conn.commit()
    if not removed:
        raise RuntimeError(f"Experiment schema cleanup was not verified: {schema}")


def run_scale(conn, scale, source_workload, selected, save):
    schema = scale["scratch_schema"]
    created = False
    try:
        print(f"\nPreparing {scale['orders_rows']:,} orders and {CUSTOMERS:,} customers...",
              flush=True)
        with conn.cursor() as cur:
            # Only a successfully created, uniquely named schema may be removed.
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created = True
        seed_dataset(conn, schema, scale["orders_rows"])
        metadata = get_metadata(schema=schema)
        entries = retarget_entries(source_workload["queries"], schema)
        workload = analyze_workload(entries, metadata)
        candidates = generate_candidates(workload, metadata)
        candidate = next((c for c in candidates
                          if c["table"] == selected["table"]
                          and c["columns"] == selected["columns"]), None)
        if candidate is None:
            raise RuntimeError("The fixed selected index is unavailable in the generated dataset.")
        scale["metadata_before"] = metadata
        scale["candidate"] = candidate
        scale["workload_queries"] = [
            {key: query[key] for key in ("id", "sql", "frequency")}
            for query in workload["queries"]
        ]
        scale["status"] = "running"
        save()

        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE")
            for trial in range(1, TRIALS + 1):
                print(f"{scale['orders_rows']:,} rows - trial {trial}/{TRIALS}", flush=True)
                result = evaluate_configuration(
                    conn, workload, [candidate], trial % 2 == 1, SEED + trial,
                )
                result.update({"trial": trial, "configuration": "fixed_selected_index"})
                scale["results"].append(result)
                save()
                before = result["baseline"]["weighted_mean_of_medians_ms"]
                after = result["indexed"]["weighted_mean_of_medians_ms"]
                size = result["total_added_index_bytes"] / (1024 * 1024)
                print(f"Baseline={before:.3f} ms | Indexed={after:.3f} ms | "
                      f"Gain={result['improvement_pct']:+.1f}% | Index size={size:.2f} MiB",
                      flush=True)
        finally:
            conn.rollback()
        scale["summary"] = summarize(
            scale["results"], {"fixed_selected_index": [candidate]},
        )[0]
    except BaseException as exc:
        scale["status"] = "failed"
        scale["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if created:
            try:
                remove_owned_schema(conn, schema)
                scale["cleanup_verified"] = True
            except Exception as cleanup_error:
                scale["cleanup_error"] = str(cleanup_error)
                if scale["status"] != "failed":
                    scale["status"] = "failed"
                    save()
                    raise
        else:
            conn.rollback()
            scale["cleanup_verified"] = True
        save()
    scale["status"] = "complete"
    save()
    print(f"{scale['orders_rows']:,} rows complete. Experiment schema removed.", flush=True)


def main():
    results_dir = PROJECT_ROOT / "results"
    metadata = get_metadata()
    entries = json.loads((PROJECT_ROOT / "data" / "workload.json").read_text(encoding="utf-8"))
    workload = analyze_workload(entries, metadata)
    recommendations = load_latest_recommendations(results_dir)
    selected = recommendations["selected_index"]
    if selected is None or (selected["schema"], selected["table"]) != (SOURCE_SCHEMA, "orders"):
        raise RuntimeError("This scale generator requires a selected index on benchmark.orders.")
    original = json.loads((results_dir / recommendations["source_report"]).read_text(encoding="utf-8"))
    selected_run = next(result for result in original["results"]
                        if identity(result["candidate"]) == identity(selected))
    if query_signature(workload["queries"]) != query_signature(selected_run["baseline"]["queries"]):
        raise RuntimeError("The workload changed since index selection. Re-run index selection first.")

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    output = results_dir / f"scale_comparison_{stamp}.json"
    report = {
        "experiment": "fixed_index_read_benefit_across_scales",
        "recorded_at_utc": now.isoformat(),
        "source_read_report": recommendations["source_report"],
        "selected_index": selected,
        "status": "incomplete", "cleanup_verified": False,
        "source_metadata_before": metadata,
        "client": {"python": platform.python_version(), "os": platform.platform()},
        "protocol": {
            "orders_rows": list(SCALES), "customer_rows": CUSTOMERS,
            "data_seed": DATA_SEED,
            "generation": "Same customer, date, status and amount generation rules as seed_data.sql",
            "dataset_storage": "Fresh committed logged tables in a unique schema per size",
            "baseline": "Primary-key indexes on customers and orders",
            "selection": "Fixed prior recommendation; not reselected or claimed optimal at each size",
            "paired_trials_per_scale": TRIALS,
            "warmup_rounds_per_phase": WARMUPS,
            "measured_rounds_per_phase": REPEATS,
            "measurement": "Frequency-weighted mean of per-query client execute + fetchall medians",
            "phase_order": "Baseline first in trials 1 and 3; indexed first in trial 2",
            "summary": "Median across trials; improvement is the median of paired percentages",
            "cache_policy": "Warmups; no forced cache clearing",
            "trial_table_fillfactor": 100,
            "trial_autovacuum": False,
            "statistics": "Explicit ANALYZE after loading each dataset",
            "scope": "Read workload timing and index size; setup time and write cost excluded",
        },
        "scales": [],
    }

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(output)

    save()
    print(f"Fixed index: orders({', '.join(selected['columns'])})")
    print(f"Results file: {output}", flush=True)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Session lock spans setup commits and measurement rollbacks.
                cur.execute("SELECT pg_try_advisory_lock(510, 1)")
                if not cur.fetchone()[0]:
                    raise RuntimeError("Another IndexMaxxer experiment is running.")
            conn.commit()
            for order_rows in SCALES:
                scale = {
                    "orders_rows": order_rows, "customer_rows": CUSTOMERS,
                    "scratch_schema": "imx_scale_" + uuid4().hex,
                    "status": "preparing", "cleanup_verified": False, "results": [],
                }
                report["scales"].append(scale)
                save()
                run_scale(conn, scale, workload, selected, save)

        report["source_metadata_after"] = get_metadata()
        report["source_index_layout_unchanged"] = (
            index_layout(metadata) == index_layout(report["source_metadata_after"])
        )
        report["cleanup_verified"] = (
            report["source_index_layout_unchanged"]
            and all(scale["cleanup_verified"] for scale in report["scales"])
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

    print("\nScale summary: medians across 3 paired trials per size")
    for scale in report["scales"]:
        result = scale["summary"]
        print(f"{scale['orders_rows']:,} orders: "
              f"baseline={result['median_baseline_ms']:.3f} ms | "
              f"indexed={result['median_indexed_ms']:.3f} ms | "
              f"paired gain={result['median_paired_improvement_pct']:+.1f}% | "
              f"index size={result['median_added_index_bytes'] / (1024 * 1024):.2f} MiB")
    print("Completed. Generated schemas removed; original index layout unchanged.")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
