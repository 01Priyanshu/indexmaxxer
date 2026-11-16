import hashlib
import json
from itertools import permutations


def _has_matching_index(table, columns):
    return any(
        index.get("method") == "btree"
        and index.get("is_valid")
        and index.get("is_ready")
        and index.get("is_partial") is False
        and index.get("has_expressions") is False
        and tuple(index.get("key_columns", [])) == columns
        for index in table["indexes"]
    )


def generate_candidates(workload, metadata):
    tables = {
        (metadata["schema"], table["table_name"]): table
        for table in metadata["tables"]
    }
    column_info = {
        key: {column["column_name"]: column for column in table["columns"]}
        for key, table in tables.items()
    }
    candidates = {}

    for query in workload["queries"]:
        groups = {}
        for role in ("where", "join", "order_by", "group_by"):
            for ref in query["features"].get(role, []):
                table_key = (ref["schema"], ref["table"])
                if table_key not in tables or ref["column"] not in column_info[table_key]:
                    raise ValueError("Query features do not match the current metadata.")
                group_key = (*table_key, ref["alias"])
                groups.setdefault(group_key, set()).add(ref["column"])

        # Keep aliases separate, including when a table is joined to itself.
        query_candidates = set()
        for (schema, table_name, alias), names in groups.items():
            table = tables[(schema, table_name)]
            for width in (1, 2):
                for columns in permutations(sorted(names), width):
                    if not _has_matching_index(table, columns):
                        query_candidates.add((schema, table_name, columns))

        # Count each query once per candidate, even across multiple aliases.
        for schema, table_name, columns in sorted(query_candidates):
            key = (schema, table_name, columns)
            if key not in candidates:
                identity = json.dumps([schema, table_name, columns])
                digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
                stats = column_info[(schema, table_name)]
                candidates[key] = {
                    "index_name": "imx_" + digest,
                    "schema": schema,
                    "table": table_name,
                    "columns": list(columns),
                    "source_query_ids": [],
                    "source_frequency": 0,
                    "estimated_table_rows": tables[(schema, table_name)]["estimated_rows"],
                    "estimated_distinct_values": {
                        name: stats[name].get("estimated_distinct_values")
                        for name in columns
                    },
                }

            candidates[key]["source_query_ids"].append(query["id"])
            candidates[key]["source_frequency"] += query["frequency"]

    return sorted(
        candidates.values(),
        key=lambda c: (
            -c["source_frequency"], c["schema"], c["table"], tuple(c["columns"])
        ),
    )
