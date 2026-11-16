import random
from statistics import median
from time import perf_counter_ns

from backend.plan_analyzer import summarize_plan


def benchmark_workload(conn, workload, warmups=2, repeats=7, seed=42):
    """Measure the analyzed benchmark workload on an existing connection."""
    if type(warmups) is not int or warmups < 0:
        raise ValueError("warmups must be a non-negative integer.")
    if type(repeats) is not int or repeats < 3:
        raise ValueError("Use at least three measured repetitions.")

    queries = workload["queries"]
    if not queries:
        raise ValueError("The workload is empty.")

    records = {
        q["id"]: {
            "id": q["id"], "sql": q["sql"], "frequency": q["frequency"],
            "samples_ms": [], "row_count": None,
        }
        for q in queries
    }
    rng = random.Random(seed)
    round_order = []

    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30s'")
        cur.execute("SET LOCAL lock_timeout = '5s'")

        for round_number in range(warmups + repeats):
            ordered = list(queries)
            rng.shuffle(ordered)
            round_order.append([q["id"] for q in ordered])

            for query in ordered:
                start = perf_counter_ns()
                cur.execute(query["sql"])
                rows = cur.fetchall()
                elapsed_ms = (perf_counter_ns() - start) / 1_000_000
                row_count = len(rows)
                del rows

                record = records[query["id"]]
                if record["row_count"] is None:
                    record["row_count"] = row_count
                elif record["row_count"] != row_count:
                    raise RuntimeError(f"{query['id']}: row count changed between runs.")

                if round_number >= warmups:
                    record["samples_ms"].append(elapsed_ms)

        # Collect plans separately, outside the ordinary latency measurements.
        for query in queries:
            record = records[query["id"]]
            samples = record["samples_ms"]
            record["median_ms"] = median(samples)
            record["min_ms"] = min(samples)
            record["max_ms"] = max(samples)

            cur.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query["sql"]
            )
            explain = cur.fetchone()[0][0]
            record["explain"] = explain
            record["plan_summary"] = summarize_plan(explain)

        setting_names = [
            "server_version", "shared_buffers", "work_mem",
            "effective_cache_size", "random_page_cost", "seq_page_cost",
            "max_parallel_workers_per_gather", "jit", "block_size",
            "track_io_timing",
        ]
        cur.execute(
            "SELECT name, setting, unit FROM pg_settings "
            "WHERE name = ANY(%s) ORDER BY name",
            (setting_names,),
        )
        settings = {
            name: {"value": value, "unit": unit}
            for name, value, unit in cur.fetchall()
        }

    results = list(records.values())
    total_frequency = sum(r["frequency"] for r in results)
    weighted_mean = sum(
        r["frequency"] * r["median_ms"] for r in results
    ) / total_frequency

    return {
        "protocol": {
            "warmup_rounds": warmups,
            "measured_rounds": repeats,
            "shuffle_seed": seed,
            "timing": "client execute + fetchall, milliseconds",
            "cache_policy": "warmup rounds; no forced cache clearing",
            "frequency_usage": "aggregation weights; equal repeats per query",
        },
        "round_order": round_order,
        "database_settings": settings,
        "queries": results,
        "weighted_mean_of_medians_ms": weighted_mean,
    }
