from time import perf_counter_ns

from psycopg2 import sql

from backend.db import get_connection
from backend.evaluation import benchmark_workload


def evaluate_candidate(workload, candidate, warmups=2, repeats=7, seed=42):
    create_index = sql.SQL(
        "CREATE INDEX {} ON {} USING btree ({})"
    ).format(
        sql.Identifier(candidate["index_name"]),
        sql.Identifier(candidate["schema"], candidate["table"]),
        sql.SQL(", ").join(sql.Identifier(c) for c in candidate["columns"]),
    )

    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE"
                )
                cur.execute("SELECT pg_try_advisory_xact_lock(510, 1)")
                if not cur.fetchone()[0]:
                    raise RuntimeError("Another index experiment is already running.")

            baseline = benchmark_workload(conn, workload, warmups, repeats, seed)

            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '60s'")
                started = perf_counter_ns()
                cur.execute(create_index)
                build_ms = (perf_counter_ns() - started) / 1_000_000

                cur.execute(
                    "SELECT pg_relation_size(c.oid) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s",
                    (candidate["schema"], candidate["index_name"]),
                )
                index_size = cur.fetchone()[0]

            indexed = benchmark_workload(conn, workload, warmups, repeats, seed)
        finally:
            # Undo CREATE INDEX on success, failure, or interruption.
            conn.rollback()

    before_counts = {q["id"]: q["row_count"] for q in baseline["queries"]}
    after_counts = {q["id"]: q["row_count"] for q in indexed["queries"]}
    if before_counts != after_counts:
        raise RuntimeError("Result row counts changed between the two configurations.")

    before_ms = baseline["weighted_mean_of_medians_ms"]
    after_ms = indexed["weighted_mean_of_medians_ms"]
    if before_ms <= 0:
        raise RuntimeError("The baseline timing must be positive.")

    used_by = [
        query["id"]
        for query in indexed["queries"]
        if any(
            node.get("index") == candidate["index_name"]
            for node in query["plan_summary"]["nodes"]
        )
    ]

    return {
        "candidate": candidate,
        "baseline": baseline,
        "indexed": indexed,
        "improvement_pct": 100 * (before_ms - after_ms) / before_ms,
        "index_size_bytes": index_size,
        "index_build_execute_ms": build_ms,
        "explain_queries_using_index": used_by,
        "row_counts_match": True,
    }
