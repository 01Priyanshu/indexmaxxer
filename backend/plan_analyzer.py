def summarize_plan(explain):
    """Summarize one EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) entry."""
    root = explain["Plan"]
    nodes = []
    observations = []

    def visit(node, depth=0):
        node_type = node["Node Type"]
        returned = node.get("Actual Rows", 0)
        removed = node.get("Rows Removed by Filter", 0)
        loops = node.get("Actual Loops", 0)

        nodes.append({
            "node_type": node_type,
            "depth": depth,
            "table": node.get("Relation Name"),
            "index": node.get("Index Name"),
            "join_type": node.get("Join Type"),
            "estimated_rows_per_loop": node.get("Plan Rows"),
            "actual_rows_per_loop": returned,
            "loops": loops,
            "rows_removed_per_loop": removed,
            "filter": node.get("Filter"),
            "index_condition": node.get("Index Cond"),
        })

        # Initial heuristic: substantial filtering with few surviving rows.
        if (
            node_type == "Seq Scan"
            and loops > 0
            and removed >= 1000
            and returned / (returned + removed) < 0.10
        ):
            table = node.get("Relation Name", "unknown table")
            observations.append(
                f"{table}: sequential scan discarded most rows. "
                "An index may help; benchmark to confirm."
            )

        for child in node.get("Plans", []):
            visit(child, depth + 1)

    visit(root)

    # Root buffer counts include child activity; do not sum all nodes.
    return {
        "execution_time_ms": explain.get("Execution Time"),
        "planning_time_ms": explain.get("Planning Time"),
        "shared_hit_blocks": root.get("Shared Hit Blocks", 0),
        "shared_read_blocks": root.get("Shared Read Blocks", 0),
        "nodes": nodes,
        "observations": observations,
    }
