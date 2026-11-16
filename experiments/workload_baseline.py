import json
import platform
from datetime import datetime, timezone

from backend.db import PROJECT_ROOT, get_connection
from backend.evaluation import benchmark_workload
from backend.metadata_analyzer import get_metadata
from backend.workload_analyzer import analyze_workload


def main():
    path = PROJECT_ROOT / "data" / "workload.json"
    entries = json.loads(path.read_text(encoding="utf-8"))

    metadata = get_metadata()
    workload = analyze_workload(entries, metadata)

    print("Running baseline: 2 warmups + 7 measured runs per query...")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
        result = benchmark_workload(conn, workload)

    now = datetime.now(timezone.utc)
    report = {
        "experiment": "existing_indexes_baseline",
        "recorded_at_utc": now.isoformat(),
        "client": {
            "python": platform.python_version(),
            "os": platform.platform(),
        },
        "metadata": metadata,
        "measurement": result,
    }

    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    output = PROJECT_ROOT / "results" / f"workload_baseline_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    for query in result["queries"]:
        print(
            f"{query['id']}: median={query['median_ms']:.3f} ms"
            f" | range={query['min_ms']:.3f}-{query['max_ms']:.3f} ms"
            f" | rows={query['row_count']}"
        )

    weighted = result["weighted_mean_of_medians_ms"]
    print(f"\nFrequency-weighted mean of query medians: {weighted:.3f} ms")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
