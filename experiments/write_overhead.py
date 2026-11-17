import json
import platform
import random
import statistics
from datetime import datetime, timezone
from time import perf_counter_ns
from uuid import uuid4

from psycopg2 import sql

from backend.db import PROJECT_ROOT, get_connection
from backend.recommendations import load_latest_recommendations

BATCH_ROWS = 1000
WARMUPS = 2
REPEATS = 7
SEED = 42
OPERATIONS = ("insert_batch", "update_customer_id", "update_total_amount")


def save_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(path)


def summarize_samples(samples):
    results = []
    for operation in OPERATIONS:
        values = {
            layout: [
                sample["elapsed_ms"]
                for sample in samples
                if sample["operation"] == operation
                and sample["layout"] == layout
                and not sample["warmup"]
            ]
            for layout in ("baseline", "indexed")
        }
        if any(len(items) != REPEATS for items in values.values()):
            raise ValueError("Incomplete write measurements.")
        before = statistics.median(values["baseline"])
        after = statistics.median(values["indexed"])
        if before <= 0:
            raise ValueError("Invalid baseline timing.")
        results.append({
            "operation": operation,
            "baseline_samples_ms": values["baseline"],
            "indexed_samples_ms": values["indexed"],
            "baseline_median_ms": before,
            "indexed_median_ms": after,
            "overhead_pct": 100 * (after / before - 1),
        })
    return results


def measure_write(conn, schema, columns, operation, indexed,
                  source_rows, target_ids, customer_max):
    trial = sql.Identifier(schema, "orders_trial")
    source = sql.Identifier(schema, "seed_orders")
    payload = sql.Identifier(schema, "payload")

    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            CREATE TABLE {} (
                order_id BIGINT PRIMARY KEY,
                customer_id INTEGER NOT NULL
                    REFERENCES benchmark.customers(customer_id),
                order_date DATE NOT NULL,
                status TEXT NOT NULL,
                total_amount NUMERIC(10,2) NOT NULL
            ) WITH (fillfactor=100, autovacuum_enabled=false)
        """).format(trial))
        cur.execute(sql.SQL("""
            INSERT INTO {}
            SELECT order_id, customer_id, order_date, status, total_amount
            FROM {} ORDER BY order_id
        """).format(trial, source))
        if cur.rowcount != source_rows:
            raise RuntimeError("Trial table did not receive all source rows.")

        if indexed:
            cur.execute(sql.SQL(
                "CREATE INDEX candidate_idx ON {} USING btree ({})"
            ).format(
                trial,
                sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            ))
        cur.execute(sql.SQL("ANALYZE {}").format(trial))
        conn.commit()

        if operation == "insert_batch":
            statement = sql.SQL("""
                INSERT INTO {}
                    (order_id, customer_id, order_date, status, total_amount)
                SELECT insert_id, customer_id, order_date, status, total_amount
                FROM {} ORDER BY order_id
            """).format(trial, payload)
            parameters = None
        elif operation == "update_customer_id":
            statement = sql.SQL("""
                UPDATE {}
                SET customer_id = CASE
                    WHEN customer_id = %s THEN 1 ELSE customer_id + 1 END
                WHERE order_id = ANY(%s)
            """).format(trial)
            parameters = (customer_max, target_ids)
        elif operation == "update_total_amount":
            statement = sql.SQL("""
                UPDATE {} SET total_amount = total_amount + 0.01
                WHERE order_id = ANY(%s)
            """).format(trial)
            parameters = (target_ids,)
        else:
            raise ValueError("Unsupported write operation.")

        started = perf_counter_ns()
        cur.execute(statement, parameters)
        affected = cur.rowcount
        conn.commit()
        elapsed_ms = (perf_counter_ns() - started) / 1_000_000

        if affected != BATCH_ROWS:
            raise RuntimeError(f"Expected {BATCH_ROWS} rows; changed {affected}.")

        cur.execute(sql.SQL("DROP TABLE {}").format(trial))
        conn.commit()
        return elapsed_ms, affected


def main():
    recommendations = load_latest_recommendations(PROJECT_ROOT / "results")
    candidate = recommendations["selected_index"]
    if candidate is None:
        raise RuntimeError("No shortlisted read candidate is available.")
    if (candidate["schema"], candidate["table"]) != ("benchmark", "orders"):
        raise RuntimeError("This write workload supports benchmark.orders.")
    allowed = {"order_id", "customer_id", "order_date", "status", "total_amount"}
    if not candidate["columns"] or not set(candidate["columns"]) <= allowed:
        raise RuntimeError("Unsupported candidate columns.")

    recorded = datetime.now(timezone.utc)
    stamp = recorded.strftime("%Y%m%dT%H%M%S%fZ")
    path = PROJECT_ROOT / "results" / f"write_comparison_{stamp}.json"
    schema = "imx_write_" + uuid4().hex
    report = {
        "experiment": "individual_index_write_comparison",
        "recorded_at_utc": recorded.isoformat(),
        "source_read_report": recommendations["source_report"],
        "candidate": candidate,
        "status": "incomplete",
        "cleanup_verified": False,
        "scratch_schema": schema,
        "batch_rows": BATCH_ROWS,
        "read_dataset_estimated_rows": recommendations["dataset_estimated_rows"],
        "client": {"python": platform.python_version(), "os": platform.platform()},
        "protocol": {
            "warmup_rounds": WARMUPS,
            "measured_rounds": REPEATS,
            "shuffle_seed": SEED,
            "timing": "Client execute plus commit; milliseconds per batch",
            "baseline_indexes": "Primary key on order_id only",
            "indexed_layout": "Primary key plus the selected candidate B-tree",
            "table_reset": "Fresh committed logged table for every sample",
            "constraints": "Primary key, NOT NULL columns, customer foreign key",
            "insert_ids": "Explicit IDs; sequence allocation is not measured",
            "fillfactor": 100,
            "trial_autovacuum": False,
            "cache_policy": "No forced cache clearing; setup outside timing",
            "order": "Shuffled operations; alternating baseline/indexed order",
            "overhead": "100 * (indexed median / baseline median - 1)",
        },
        "samples": [],
    }
    save_report(path, report)
    print(f"Testing: orders({', '.join(candidate['columns'])})")
    print(f"Batch: {BATCH_ROWS} rows; {WARMUPS} warmups + {REPEATS} measured rounds.")
    print(f"Results file: {path}", flush=True)

    try:
        with get_connection() as conn:
            created = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = '120s'")
                    cur.execute("SET lock_timeout = '5s'")
                    cur.execute("SET synchronous_commit = on")
                    cur.execute("SELECT pg_try_advisory_lock(510, 1)")
                    if not cur.fetchone()[0]:
                        raise RuntimeError("Another IndexMaxxer experiment is running.")
                    cur.execute(
                        "SELECT name, setting, unit FROM pg_settings "
                        "WHERE name = ANY(%s)",
                        (["server_version", "shared_buffers", "work_mem",
                          "wal_level", "fsync", "full_page_writes",
                          "synchronous_commit", "block_size"],),
                    )
                    report["database_settings"] = {
                        name: {"value": value, "unit": unit}
                        for name, value, unit in cur.fetchall()
                    }
                    cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                    created = True
                    source = sql.Identifier(schema, "seed_orders")
                    payload = sql.Identifier(schema, "payload")
                    cur.execute(sql.SQL("""
                        CREATE TABLE {} AS
                        SELECT order_id, customer_id, order_date, status, total_amount
                        FROM benchmark.orders ORDER BY order_id
                    """).format(source))
                    cur.execute(sql.SQL(
                        "SELECT count(*), max(order_id) FROM {}"
                    ).format(source))
                    source_rows, maximum_id = cur.fetchone()
                    if source_rows < BATCH_ROWS:
                        raise RuntimeError("Not enough source orders for this batch.")
                    report["source_rows"] = source_rows
                    cur.execute(sql.SQL("""
                        CREATE TABLE {} AS
                        SELECT *, %s::bigint + row_number() OVER (ORDER BY order_id)
                            AS insert_id
                        FROM {} ORDER BY order_id LIMIT %s
                    """).format(payload, source), (maximum_id, BATCH_ROWS))
                    cur.execute(sql.SQL(
                        "SELECT order_id FROM {} ORDER BY order_id"
                    ).format(payload))
                    target_ids = [row[0] for row in cur.fetchall()]
                    cur.execute(
                        "SELECT count(*), min(customer_id), max(customer_id) "
                        "FROM benchmark.customers"
                    )
                    customers, minimum, maximum = cur.fetchone()
                    if customers < 2 or minimum != 1 or maximum != customers:
                        raise RuntimeError("This workload expects customer IDs 1 through N.")
                    report["customer_rows"] = customers
                    conn.commit()
                save_report(path, report)

                rng = random.Random(SEED)
                for round_index in range(WARMUPS + REPEATS):
                    operations = list(OPERATIONS)
                    rng.shuffle(operations)
                    layouts = ["baseline", "indexed"]
                    if round_index % 2:
                        layouts.reverse()
                    for operation in operations:
                        for layout in layouts:
                            elapsed, affected = measure_write(
                                conn, schema, candidate["columns"], operation,
                                layout == "indexed", source_rows, target_ids, maximum,
                            )
                            report["samples"].append({
                                "round": round_index + 1,
                                "warmup": round_index < WARMUPS,
                                "operation": operation,
                                "layout": layout,
                                "elapsed_ms": elapsed,
                                "rows_affected": affected,
                            })
                    save_report(path, report)
                    phase = "warmup" if round_index < WARMUPS else "measured"
                    print(f"Round {round_index + 1}/{WARMUPS + REPEATS}: {phase} complete",
                          flush=True)
            finally:
                conn.rollback()
                if created:
                    with conn.cursor() as cur:
                        cur.execute(sql.SQL(
                            "DROP SCHEMA IF EXISTS {} CASCADE"
                        ).format(sql.Identifier(schema)))
                    conn.commit()
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
                            (schema,),
                        )
                        report["cleanup_verified"] = cur.fetchone()[0] == 0

        if not report["cleanup_verified"]:
            raise RuntimeError("Scratch schema cleanup was not verified.")
        report["operations"] = summarize_samples(report["samples"])
        report["status"] = "complete"
        save_report(path, report)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        save_report(path, report)
        raise

    for result in report["operations"]:
        print(
            f"{result['operation']}: "
            f"baseline={result['baseline_median_ms']:.3f} ms | "
            f"indexed={result['indexed_median_ms']:.3f} ms | "
            f"overhead={result['overhead_pct']:+.1f}%"
        )
    print("Completed. Experiment schema removed.")
    print(f"Saved to: {path}")


if __name__ == "__main__":
    main()
