import json
import logging
import re

from fastapi import APIRouter, HTTPException

from backend.db import PROJECT_ROOT

router = APIRouter(prefix="/api/comparisons", tags=["Experiment results"])
logger = logging.getLogger(__name__)


def pair_queries(result, index_name):
    baseline_queries = result["baseline"]["queries"]
    indexed_queries = result["indexed"]["queries"]

    before = {query["id"]: query for query in baseline_queries}
    after = {query["id"]: query for query in indexed_queries}

    if (
        len(before) != len(baseline_queries)
        or len(after) != len(indexed_queries)
        or before.keys() != after.keys()
    ):
        raise ValueError("Query IDs do not match between measurements.")

    paired = []

    for query_id, baseline in before.items():
        indexed = after[query_id]

        if (
            baseline["sql"] != indexed["sql"]
            or baseline["frequency"] != indexed["frequency"]
        ):
            raise ValueError("Query SQL or frequency changed between measurements.")

        baseline_ms = baseline["median_ms"]
        indexed_ms = indexed["median_ms"]

        paired.append({
            "id": query_id,
            "sql": baseline["sql"],
            "frequency": baseline["frequency"],
            "baseline": baseline,
            "indexed": indexed,
            "improvement_pct": (
                100 * (baseline_ms - indexed_ms) / baseline_ms
                if baseline_ms > 0 else None
            ),
            "row_counts_match": (
                baseline["row_count"] == indexed["row_count"]
            ),
            "index_used": any(
                node.get("index") == index_name
                for node in indexed["plan_summary"]["nodes"]
            ),
        })

    return paired


@router.get("/{report_name}/{index_name}")
def comparison_details(report_name: str, index_name: str):
    if not re.fullmatch(
        r"index_comparison_[0-9]{8}T[0-9]{12}Z\.json", report_name
    ):
        raise HTTPException(status_code=404, detail="Report not found.")

    path = PROJECT_ROOT / "results" / report_name

    try:
        report = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(report, dict):
            raise ValueError("Invalid report format.")

        if (
            report.get("status") != "complete"
            or not report.get("cleanup_verified")
        ):
            raise HTTPException(
                status_code=409,
                detail="This comparison is not complete with verified cleanup.",
            )

        result = next(
            (
                item for item in report["results"]
                if item["candidate"]["index_name"] == index_name
            ),
            None,
        )

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="This index was not tested in the selected report.",
            )

        queries = pair_queries(result, index_name)

        return {
            "source_report": report_name,
            "recorded_at_utc": report["recorded_at_utc"],
            "candidate": result["candidate"],
            "index_size_bytes": result["index_size_bytes"],
            "baseline_protocol": result["baseline"]["protocol"],
            "indexed_protocol": result["indexed"]["protocol"],
            "query_count": len(queries),
            "queries": queries,
            "timing_note": (
                "Query medians measure client execute + fetchall. "
                "Execution plans and buffer metrics come from separate "
                "EXPLAIN diagnostic runs."
            ),
        }

    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Report not found."
        ) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.exception("Could not read comparison details")
        raise HTTPException(
            status_code=500,
            detail="The saved comparison report could not be read.",
        ) from exc
