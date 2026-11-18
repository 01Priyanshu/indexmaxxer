"""Compare a fixed index on uniform and skewed synthetic customer distributions.

Uses isolated, committed tables. Run: python -m experiments.compare_skew
"""

import csv
import hashlib
import io
import json
import platform
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from psycopg2 import sql

from backend.db import PROJECT_ROOT, get_connection
from backend.index_advisor import generate_candidates
from backend.metadata_analyzer import get_metadata
from backend.recommendations import load_latest_recommendations
from backend.workload_analyzer import analyze_workload
from experiments.compare_indexes import index_layout
from experiments.compare_scales import remove_owned_schema, retarget_entries
from experiments.compare_strategies import (
    REPEATS, SEED, TRIALS, WARMUPS, evaluate_configuration,
    identity, query_signature, summarize,
)

CUSTOMERS = 10_000
ORDERS = 100_000
HOT_CUSTOMERS = 100
HOT_SHARE = 0.80


def generated_orders(distribution, count=ORDERS):
    if distribution not in {"uniform", "skewed"}:
        raise ValueError("Unknown distribution")
    customer_rng = random.Random(42)
    other_rng = random.Random(43)
    first_date = date(2025, 1, 1)
    statuses = ("pending", "shipped", "delivered", "cancelled")
    for _ in range(count):
        u = customer_rng.random()
        if distribution == "uniform":
            customer = 1 + int(u * CUSTOMERS)
        elif u < HOT_SHARE:
            customer = 1 + int(u / HOT_SHARE * HOT_CUSTOMERS)
        else:
            customer = HOT_CUSTOMERS + 1 + int(
                (u - HOT_SHARE) / (1 - HOT_SHARE) * (CUSTOMERS - HOT_CUSTOMERS)
            )
        day = (first_date + timedelta(days=other_rng.randrange(730))).isoformat()
        status = statuses[other_rng.randrange(4)]
        amount = f"{other_rng.randrange(1000, 100001) / 100:.2f}"
        yield customer, day, status, amount


def seed(conn, schema, distribution):
    customers = sql.Identifier(schema, "customers")
    orders = sql.Identifier(schema, "orders")
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    ancillary_hash = hashlib.sha256()
    for row in generated_orders(distribution):
        writer.writerow(row)
        ancillary_hash.update(("|".join(row[1:]) + "\n").encode("ascii"))
    stream.seek(0)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '180s'")
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute(sql.SQL("""
            CREATE TABLE {} (
                customer_id INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL, region TEXT NOT NULL
            ) WITH (fillfactor=100, autovacuum_enabled=false)
        """).format(customers))
        cur.execute(sql.SQL("""
            CREATE TABLE {} (
                order_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES {}(customer_id),
                order_date DATE NOT NULL, status TEXT NOT NULL,
                total_amount NUMERIC(10,2) NOT NULL
            ) WITH (fillfactor=100, autovacuum_enabled=false)
        """).format(orders, customers))
        cur.execute(sql.SQL("""
            INSERT INTO {} (customer_id, customer_name, region)
            SELECT id, 'Customer ' || id,
                   (ARRAY['North','South','East','West'])[1 + ((id - 1) %% 4)]
            FROM generate_series(1, %s) AS g(id)
        """).format(customers), (CUSTOMERS,))
        statement = sql.SQL("COPY {} (customer_id, order_date, status, total_amount) FROM STDIN WITH (FORMAT CSV)").format(orders)
        cur.copy_expert(statement.as_string(conn), stream)
        cur.execute(sql.SQL("ANALYZE {}").format(customers))
        cur.execute(sql.SQL("ANALYZE {}").format(orders))
        cur.execute(sql.SQL("""
            SELECT count(*), count(*) FILTER (WHERE customer_id <= %s),
                   count(DISTINCT customer_id), min(customer_id), max(customer_id)
            FROM {}
        """).format(orders), (HOT_CUSTOMERS,))
        count, hot, distinct, smallest, largest = cur.fetchone()
        if count != ORDERS or not (1 <= smallest <= largest <= CUSTOMERS):
            raise RuntimeError("Generated data failed validation.")
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(customers))
        if cur.fetchone()[0] != CUSTOMERS:
            raise RuntimeError("Unexpected customer count.")
    conn.commit()
    return {"orders_rows": count, "customers_rows": CUSTOMERS,
            "orders_for_first_100_customers": hot,
            "actual_hot_customer_share_pct": 100 * hot / count,
            "distinct_customers_with_orders": distinct,
            "non_customer_columns_sha256": ancillary_hash.hexdigest()}


def per_query_summary(results, candidate):
    output = []
    ids = [q["id"] for q in results[0]["baseline"]["queries"]]
    for qid in ids:
        before, after, gains, counts, used = [], [], [], [], 0
        for result in results:
            b = next(q for q in result["baseline"]["queries"] if q["id"] == qid)
            a = next(q for q in result["indexed"]["queries"] if q["id"] == qid)
            before.append(b["median_ms"])
            after.append(a["median_ms"])
            gains.append(100 * (b["median_ms"] - a["median_ms"]) / b["median_ms"])
            counts.append(b["row_count"])
            used += int(any(n.get("index") == candidate["index_name"]
                            for n in a["plan_summary"]["nodes"]))
        output.append({"id": qid, "baseline_ms_by_trial": before,
                       "indexed_ms_by_trial": after, "paired_gains_pct": gains,
                       "median_baseline_ms": statistics.median(before),
                       "median_indexed_ms": statistics.median(after),
                       "median_paired_improvement_pct": statistics.median(gains),
                       "row_counts_by_trial": counts,
                       "diagnostic_trials_using_index": used})
    return output


def main():
    results_dir = PROJECT_ROOT / "results"
    metadata = get_metadata()
    entries = json.loads((PROJECT_ROOT / "data" / "workload.json").read_text(encoding="utf-8"))
    workload = analyze_workload(entries, metadata)
    recommendations = load_latest_recommendations(results_dir)
    selected = recommendations["selected_index"]
    if selected is None or identity(selected) != ("benchmark", "orders", ("customer_id",)):
        raise RuntimeError("This experiment expects the selected orders(customer_id) index.")
    original = json.loads((results_dir / recommendations["source_report"]).read_text(encoding="utf-8"))
    selected_run = next(r for r in original["results"] if identity(r["candidate"]) == identity(selected))
    if query_signature(workload["queries"]) != query_signature(selected_run["baseline"]["queries"]):
        raise RuntimeError("The workload changed since the recorded selection.")

    now = datetime.now(timezone.utc)
    output = results_dir / f"skew_comparison_{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    report = {
        "experiment": "fixed_index_uniform_vs_skewed_data",
        "recorded_at_utc": now.isoformat(), "status": "incomplete",
        "cleanup_verified": False, "source_read_report": recommendations["source_report"],
        "selected_index": selected, "source_metadata_before": metadata,
        "client": {"python": platform.python_version(), "os": platform.platform()},
        "protocol": {
            "orders_rows": ORDERS, "customer_rows": CUSTOMERS,
            "uniform": "Each customer has equal probability of receiving an order",
            "skewed": "80% probability across customers 1-100; 20% across 101-10000",
            "data_seeds": {"customer": 42, "other_columns": 43},
            "controlled_columns": "Identical order dates, statuses, amounts and insertion order in both datasets",
            "selection": "Fixed prior customer_id index; no re-optimization by distribution",
            "trials": TRIALS, "warmups": WARMUPS, "repeats": REPEATS,
            "phase_order": "Baseline first in trials 1 and 3; indexed first in trial 2",
            "distribution_order": "Seeded shuffle within each trial",
            "measurement": "Frequency-weighted mean of per-query execute + fetchall medians",
            "summary": "Median of per-trial metrics; gain is median paired percentage",
            "cache": "Warmups; no forced cache clearing",
            "statistics": "ANALYZE after loading; autovacuum disabled on experiment tables only",
            "limitations": [
                "Same query literals can return different row counts across distributions.",
                "Existing workload emphasizes low customer IDs, which are hot in the skewed dataset.",
                "Timing differences without diagnostic index use do not establish an index benefit.",
                "Three trials on one machine do not establish statistical significance.",
            ],
        },
        "trial_order": [], "distributions": [],
    }

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(output)

    results_dir.mkdir(parents=True, exist_ok=True)
    save()
    print(f"Results file: {output}", flush=True)
    try:
        with get_connection() as conn:
            owned = []
            prepared = {}
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(510, 1)")
                    if not cur.fetchone()[0]:
                        raise RuntimeError("Another IndexMaxxer experiment is running.")
                conn.commit()
                for name in ("uniform", "skewed"):
                    schema = "imx_skew_" + uuid4().hex
                    item = {"distribution": name, "scratch_schema": schema,
                            "status": "preparing", "cleanup_verified": False, "results": []}
                    report["distributions"].append(item)
                    with conn.cursor() as cur:
                        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                    owned.append(item)
                    print(f"Preparing {name}: {ORDERS:,} orders...", flush=True)
                    item["data_profile"] = seed(conn, schema, name)
                    meta = get_metadata(schema=schema)
                    work = analyze_workload(retarget_entries(workload["queries"], schema), meta)
                    candidate = next(c for c in generate_candidates(work, meta)
                                     if c["table"] == "orders" and c["columns"] == ["customer_id"])
                    item.update({"metadata_before": meta, "candidate": candidate,
                                 "workload_queries": [{k: q[k] for k in ("id", "sql", "frequency")}
                                                      for q in work["queries"]],
                                 "status": "running"})
                    prepared[name] = (item, work, candidate)
                    print(f"  Orders belonging to first 100 customers: {item['data_profile']['actual_hot_customer_share_pct']:.2f}%")
                    save()
                hashes = {item["data_profile"]["non_customer_columns_sha256"] for item in owned}
                if len(hashes) != 1:
                    raise RuntimeError("Non-customer columns differ between datasets.")
                report["controlled_columns_match"] = True
                rng = random.Random(SEED)
                for trial in range(1, TRIALS + 1):
                    names = list(prepared)
                    rng.shuffle(names)
                    report["trial_order"].append({"trial": trial, "distributions": names})
                    for name in names:
                        item, work, candidate = prepared[name]
                        print(f"\nTrial {trial}/{TRIALS}: {name}", flush=True)
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE")
                            result = evaluate_configuration(conn, work, [candidate], trial % 2 == 1, SEED + trial)
                        finally:
                            conn.rollback()
                        result.update({"trial": trial, "configuration": "fixed_selected_index"})
                        item["results"].append(result)
                        save()
                        print(f"Baseline={result['baseline']['weighted_mean_of_medians_ms']:.3f} ms | "
                              f"Indexed={result['indexed']['weighted_mean_of_medians_ms']:.3f} ms | "
                              f"Gain={result['improvement_pct']:+.1f}%", flush=True)
                for item, work, candidate in prepared.values():
                    item["summary"] = summarize(item["results"], {"fixed_selected_index": [candidate]})[0]
                    item["query_summary"] = per_query_summary(item["results"], candidate)
                    item["status"] = "complete"
            finally:
                errors = []
                for item in reversed(owned):
                    try:
                        remove_owned_schema(conn, item["scratch_schema"])
                        item["cleanup_verified"] = True
                    except Exception as exc:
                        item["cleanup_error"] = str(exc)
                        errors.append(str(exc))
                save()
                if errors:
                    raise RuntimeError("Experiment schema cleanup failed: " + "; ".join(errors))
        report["source_metadata_after"] = get_metadata()
        report["source_index_layout_unchanged"] = index_layout(metadata) == index_layout(report["source_metadata_after"])
        report["cleanup_verified"] = report["source_index_layout_unchanged"] and all(
            item["cleanup_verified"] for item in report["distributions"])
        if not report["cleanup_verified"]:
            raise RuntimeError("Cleanup or original index-layout verification failed.")
        report["status"] = "complete"
        save()
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        save()
        raise

    print("\nDistribution summary: medians across 3 paired trials")
    for item in report["distributions"]:
        s = item["summary"]
        print(f"{item['distribution']}: baseline={s['median_baseline_ms']:.3f} ms | "
              f"indexed={s['median_indexed_ms']:.3f} ms | "
              f"paired gain={s['median_paired_improvement_pct']:+.1f}% | "
              f"index size={s['median_added_index_bytes'] / 1048576:.2f} MiB")
        for q in item["query_summary"]:
            print(f"  {q['id']}: rows={q['row_counts_by_trial'][0]:,} | "
                  f"paired gain={q['median_paired_improvement_pct']:+.1f}% | "
                  f"index in diagnostic plans={q['diagnostic_trials_using_index']}/{TRIALS}")
    print("Completed. Generated schemas removed; original index layout unchanged.")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
