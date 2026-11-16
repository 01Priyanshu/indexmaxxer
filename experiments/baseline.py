import json
from datetime import datetime, timezone

from backend.db import PROJECT_ROOT, get_connection

QUERY = """
SELECT *
FROM benchmark.orders
WHERE customer_id = 2
  AND order_date >= DATE '2026-01-01'
"""


def main():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + QUERY
            )
            explain = cur.fetchone()[0][0]

    plan = explain["Plan"]

    result = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "measurement_type": "single diagnostic run",
        "query": QUERY.strip(),
        "explain": explain,
    }

    output = PROJECT_ROOT / "results" / "baseline_plan.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"Top plan node: {plan['Node Type']}")
    print(f"Execution time (EXPLAIN): {explain['Execution Time']:.3f} ms")
    print(f"Rows returned: {plan['Actual Rows']}")
    print(f"Shared buffer hits: {plan.get('Shared Hit Blocks', 0)}")
    print(f"Shared blocks read: {plan.get('Shared Read Blocks', 0)}")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()

