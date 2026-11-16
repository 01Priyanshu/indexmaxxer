from backend.query_analyzer import analyze_query


def analyze_workload(entries, metadata):
    """Count shared templates while retaining every query for benchmarking."""
    if not isinstance(entries, list) or not entries:
        raise ValueError("The workload must be a non-empty list.")

    queries = []
    patterns = {}
    used_ids = set()

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each workload entry must be an object.")

        query_id = entry.get("id")
        frequency = entry.get("frequency", 1)
        sql = entry.get("sql")

        if not isinstance(query_id, str) or not query_id.strip() or query_id in used_ids:
            raise ValueError("Each query needs a non-empty, unique string ID.")
        if type(frequency) is not int or frequency < 1:
            raise ValueError(f"{query_id}: frequency must be a positive integer.")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError(f"{query_id}: SQL must be a non-empty string.")

        analysis = analyze_query(sql, metadata)
        queries.append({"id": query_id, "frequency": frequency, **analysis})
        used_ids.add(query_id)

        pattern = patterns.setdefault(analysis["template"], {
            "template": analysis["template"],
            "frequency": 0,
            "query_ids": [],
        })
        pattern["frequency"] += frequency
        pattern["query_ids"].append(query_id)

    return {
        "total_frequency": sum(q["frequency"] for q in queries),
        "queries": queries,
        "patterns": sorted(
            patterns.values(), key=lambda p: p["frequency"], reverse=True
        ),
    }
