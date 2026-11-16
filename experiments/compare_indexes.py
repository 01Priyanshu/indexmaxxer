import json
import platform
import random
from datetime import datetime, timezone

from backend.db import PROJECT_ROOT
from backend.metadata_analyzer import get_metadata
from backend.workload_analyzer import analyze_workload
from backend.index_advisor import generate_candidates
from backend.candidate_evaluation import evaluate_candidate


def index_layout(metadata):
    return sorted(
        (metadata["schema"], table["table_name"], index["definition"])
        for table in metadata["tables"]
        for index in table["indexes"]
    )


def main():
    path = PROJECT_ROOT / "data" / "workload.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    metadata = get_metadata()
    workload = analyze_workload(entries, metadata)
    candidates = generate_candidates(workload, metadata)

    if not candidates:
        raise RuntimeError("No new index candidates to test.")

    random.Random(42).shuffle(candidates)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    output = PROJECT_ROOT / "results" / f"index_comparison_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "experiment": "individual_index_comparison",
        "recorded_at_utc": now.isoformat(),
        "status": "incomplete",
        "method": "Paired baseline/index measurements; index creation rolled back after each test.",
        "client": {
            "python": platform.python_version(),
            "os": platform.platform(),
        },
        "metadata_before": metadata,
        "candidate_order": [c["index_name"] for c in candidates],
        "results": [],
    }

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(output)

    save()
    print(f"Results file: {output}")

    for number, candidate in enumerate(candidates, start=1):
        label = f"{candidate['table']}({', '.join(candidate['columns'])})"
        print(f"\nTesting {number}/{len(candidates)}: {label}", flush=True)

        result = evaluate_candidate(workload, candidate)
        report["results"].append(result)
        save()

        before = result["baseline"]["weighted_mean_of_medians_ms"]
        after = result["indexed"]["weighted_mean_of_medians_ms"]
        size_mib = result["index_size_bytes"] / (1024 * 1024)

        print(
            f"Baseline={before:.3f} ms | Indexed={after:.3f} ms"
            f" | Improvement={result['improvement_pct']:+.1f}%"
            f" | Index size={size_mib:.2f} MiB"
        )
        used_by = ", ".join(result["explain_queries_using_index"]) or "none"
        print(f"Index used in diagnostic plans for: {used_by}")

    report["metadata_after"] = get_metadata()
    report["cleanup_verified"] = (
        index_layout(metadata) == index_layout(report["metadata_after"])
    )
    if not report["cleanup_verified"]:
        save()
        raise RuntimeError("The index layout changed unexpectedly; inspect the saved report.")

    report["status"] = "complete"
    save()
    print("\nCompleted. Original index layout restored.")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
