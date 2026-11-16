import json

from backend.db import PROJECT_ROOT
from backend.recommendations import load_latest_recommendations


def main():
    result = load_latest_recommendations(PROJECT_ROOT / "results")

    print(f"Source: {result['source_report']}\n")

    for position, item in enumerate(result["rankings"], start=1):
        label = f"{item['table']}({', '.join(item['columns'])})"
        decision = "SHORTLIST" if item["eligible"] else "FILTERED"
        size_mib = item["index_size_bytes"] / (1024 * 1024)

        print(
            f"{position}. {label} | {decision}"
            f" | gain={item['improvement_pct']:+.1f}%"
            f" | size={size_mib:.2f} MiB"
        )
        print(f"   {item['reason']}")

    selected = result["selected_index"]
    if selected:
        columns = ", ".join(selected["columns"])
        print(f"\nBest single index in this read test: {selected['table']}({columns})")
    else:
        print("\nNo candidate passed the configured checks.")

    print("Write overhead: not evaluated yet.")

    filename = result["source_report"].replace(
        "index_comparison_", "recommendations_", 1
    )
    output = PROJECT_ROOT / "results" / filename
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Saved to: {output}")


if __name__ == "__main__":
    main()
